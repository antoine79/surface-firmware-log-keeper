//! firmware-log-report (SPEC section 8): reads the archive the keeper writes and prints what it holds. It writes
//! nothing, anywhere.
#![forbid(unsafe_code)]
use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use firmware_logs::firmware_memory_log;
use firmware_logs::archive_reader::{folder_key, FILE_ONLY};
use firmware_logs::log_format::{parse_log, Listing, Row, Source};

/// println! that stops the report, exit status 1, when its output cannot be written (a reader such as `head` that
/// went away, a full disk) instead of panicking.
macro_rules! say {
    ($($arg:tt)*) => {
        if writeln!(io::stdout(), $($arg)*).is_err() {
            std::process::exit(1)
        }
    };
}

const USAGE: &str = "usage: firmware-log-report [--stage-over SECONDS] [--repeat-over COUNT]
       firmware-log-report timeline
       firmware-log-report decode-advlog FILE";

// On the Surface Book 2 a normal firmware run hands over to the boot loader at about 11 s and repeats no line more
// than 8 times (278 runs, Nov 2026).
const STAGE_OVER_S: f64 = 30.0;
const REPEAT_OVER: usize = 50;

/// A root as the keeper reads it (SPEC 6): a variable set but empty counts as unset.
fn root(name: &str, default: &str) -> PathBuf {
    std::env::var_os(name).filter(|v| !v.is_empty()).map_or_else(|| default.into(), PathBuf::from)
}

/// What the report could not read. Each is printed as an `error:` line and makes the exit status 1.
#[derive(Default)]
struct Errors(Vec<String>);

impl Errors {
    fn add(&mut self, path: &Path, e: impl std::fmt::Display) {
        self.0.push(format!("error: {}: {e}", path.display()));
    }

    fn finish(self) -> i32 {
        self.0.iter().for_each(|e| say!("{e}"));
        i32::from(!self.0.is_empty())
    }
}

/// A run folder of the archive, as far as it could be read.
struct Run {
    name: String,
    key: String,
    listing: Listing,
    rows: Vec<Row>,
    fpdt: String,
}

fn read(path: &Path, errors: &mut Errors) -> Option<Vec<u8>> {
    fs::read(path).map_err(|e| errors.add(path, e)).ok()
}

fn runs(merged: &Path, errors: &mut Errors) -> Vec<Run> {
    let names = match fs::read_dir(merged) {
        Ok(dir) => dir.filter_map(|e| e.ok()?.file_name().into_string().ok()).collect::<Vec<_>>(),
        Err(e) => {
            errors.add(merged, e);
            return Vec::new();
        }
    };
    let mut runs: Vec<Run> = names.into_iter().filter_map(|name| {
        let key = folder_key(&name)?.to_string();
        let dir = merged.join(&name);
        let listing = read(&dir.join("sources.tsv"), errors)
            .and_then(|d| Listing::parse(&d).map_err(|e| errors.add(&dir.join("sources.tsv"), e)).ok());
        let rows = read(&dir.join("firmware.log"), errors)
            .and_then(|d| parse_log(&d).map_err(|e| errors.add(&dir.join("firmware.log"), e)).ok());
        let fpdt = fs::read_to_string(dir.join("fpdt-boot.txt")).unwrap_or_default();
        Some(Run { name, key, listing: listing.unwrap_or_default(), rows: rows.unwrap_or_default(), fpdt })
    }).collect();
    runs.sort_by(|a, b| a.name.cmp(&b.name));
    runs
}

fn secs(ms: i64) -> String {
    format!("{}{}.{:03}", if ms < 0 { "-" } else { "" }, ms.unsigned_abs() / 1000, ms.unsigned_abs() % 1000)
}

fn dash(v: Option<impl ToString>) -> String {
    v.map_or("-".into(), |v| v.to_string())
}

/// Milliseconds from reset to the hand-over to the boot loader: the file log is written then, so its last line
/// ends the firmware stage; without a file log, the boot timings' boot loader start.
fn stage_ms(run: &Run) -> Option<i64> {
    let file = run.rows.iter().filter(|r| r.source.file_line().is_some()).filter_map(|r| r.uptime).max();
    file.or_else(|| {
        let ns = run.fpdt.lines().find_map(|l| l.strip_prefix("bootloader_launch_ns="))?;
        Some(ns.trim().parse::<i64>().ok()? / 1_000_000)
    })
}

fn source_name(s: &Source) -> &'static str {
    match s {
        Source::Bootefi { .. } => "bootefi",
        Source::Both { .. } => "both",
        Source::Errrec { .. } => "errrec",
        Source::Memory { .. } => "memory",
    }
}

fn run_line(run: &Run) -> String {
    let l = &run.listing;
    let mut sources = Vec::new();
    if l.memory.is_some() {
        sources.push("memory".to_string());
    }
    if !l.files.is_empty() {
        sources.push(format!("{} file", l.files.len()));
    }
    if !l.records.is_empty() {
        sources.push(format!("{} records", l.records.len()));
    }
    let mut counts = BTreeMap::new();
    run.rows.iter().for_each(|r| *counts.entry(source_name(&r.source)).or_insert(0) += 1);
    let rows: Vec<String> = counts.iter().map(|(s, n)| format!("{s} {n}")).collect();
    // with both copies, the memory rows are the lines of one copy only (a file log holds no line memory lacks)
    let single = (l.memory.is_some() && !l.files.is_empty()).then(|| counts.get("memory").copied().unwrap_or(0));
    format!("{:<50}  {:<26}  {:<30}  {:>6}  {:>9}", run.name, sources.join(", "), rows.join(" "), dash(single),
            dash(stage_ms(run).map(secs)))
}

/// One keeper run's lines of firmware_bootefilogs/seen.tsv: its UTC time, its boot, the UEFI_Index it read, and
/// how many contents it was the first to see.
struct Sighting {
    utc: String,
    boot: String,
    index: Option<i64>,
    new: usize,
}

/// The keeper runs of seen.tsv in order, and for each content hash the run that saw it first.
fn sightings(seen: &str) -> (Vec<Sighting>, BTreeMap<String, usize>) {
    let (mut runs, mut first) = (Vec::<Sighting>::new(), BTreeMap::new());
    for line in seen.lines() {
        let f: Vec<&str> = line.split('\t').collect();
        let [utc, boot, _, hash, .., index] = f[..] else { continue };
        if runs.last().is_none_or(|r| (r.utc.as_str(), r.boot.as_str()) != (utc, boot)) {
            runs.push(Sighting { utc: utc.into(), boot: boot.into(), index: index.parse().ok(), new: 0 });
        }
        if !first.contains_key(hash) {
            first.insert(hash.to_string(), runs.len() - 1);
            runs.last_mut().unwrap().new += 1;
        }
    }
    (runs, first)
}

/// The firmware keeps 9 file logs and UEFI_Index names the slot it wrote last, so between two keeper runs it wrote
/// as many logs as the index moved, modulo 9. Fewer new contents than that: the others were overwritten unseen.
fn gap(before: &Sighting, after: &Sighting) -> Option<String> {
    let steps = (after.index? - before.index?).rem_euclid(9) as usize;
    let span = format!("between the keeper runs {} and {}", before.utc, after.utc);
    match after.new {
        // every slot was rewritten: the firmware wrote 9 + steps logs, or 9 more for each further lap
        9 => Some(format!("{span}: all 9 file logs were new, {steps} or more were overwritten unseen")),
        new if steps > new => Some(format!("{span}: the firmware wrote {steps} file logs, {} were overwritten unseen",
                                            steps - new)),
        _ => None,
    }
}

/// Every error record: its row (decoded, in a run or in unattached/) and when it was archived and deleted, from
/// hwerrrec/seen.tsv.
fn records(runs: &[Run], unattached: &[Row], merged: &Path, errors: &mut Errors) {
    let seen_path = merged.join("hwerrrec").join("seen.tsv");
    let seen = match fs::read(&seen_path) {
        Ok(d) => String::from_utf8_lossy(&d).into_owned(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => String::new(),
        Err(e) => {
            errors.add(&seen_path, e);
            String::new()
        }
    };
    // hash -> (archived, deleted or kept): the first time of each action
    let mut events = BTreeMap::<&str, BTreeMap<String, String>>::new();
    for f in seen.lines().map(|l| l.split('\t').collect::<Vec<_>>()) {
        if let [utc, _, action, _, hash, reason] = f[..] {
            let what = if action == "kept" { format!("kept ({reason})") } else { action.to_string() };
            events.entry(hash).or_default().entry(what).or_insert_with(|| utc.to_string());
        }
    }
    let rows = runs.iter().flat_map(|r| r.rows.iter().map(move |row| (r.name.as_str(), row)))
        .chain(unattached.iter().map(|row| ("unattached", row)));
    let mut list: Vec<(Option<firmware_logs::dates::Time>, String, String)> = Vec::new();
    let mut with_row = std::collections::HashSet::new();
    for (place, row) in rows {
        if let Some(hash) = row.source.record() {
            with_row.insert(hash.to_string());
            list.push((row.time, hash.to_string(), format!("in {place}  {}", row.text)));
        }
    }
    for hash in events.keys().filter(|h| !with_row.contains(**h)) {
        list.push((None, hash.to_string(), "no row yet (the next keeper run writes it)".into()));
    }
    list.sort();
    say!("\nerror records: {} ({} attached to no run)", list.len(), unattached.len());
    for (time, hash, what) in &list {
        let history: Vec<String> = events.get(hash.as_str()).into_iter().flatten().map(|(a, t)| format!("{a} {t}"))
            .collect();
        let history = if history.is_empty() { "not in hwerrrec/seen.tsv".to_string() } else { history.join(", ") };
        say!("  {}  {history}  {what}", dash(*time));
    }
}

fn summary(stage_over: f64, repeat_over: usize) -> i32 {
    let mut errors = Errors::default();
    let (memory, bootefi, merged) = (root("FWLK_MEMORY_ROOT", "/var/log/firmware_memory"),
                                     root("FWLK_BOOTEFI_ROOT", "/var/log/firmware_bootefilogs"),
                                     root("FWLK_MERGED_ROOT", "/var/log/firmware"));
    let runs = runs(&merged, &mut errors);
    let seen_path = bootefi.join("seen.tsv");
    let seen = match fs::read(&seen_path) {
        Ok(d) => String::from_utf8_lossy(&d).into_owned(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => String::new(),
        Err(e) => {
            errors.add(&seen_path, e);
            String::new()
        }
    };
    let (keeper_runs, first) = sightings(&seen);

    let file_only: Vec<&Run> = runs.iter().filter(|r| r.key.starts_with(FILE_ONLY)).collect();
    say!("firmware runs: {} ({} reached Linux, {} known by their file log only)", runs.len(),
             runs.len() - file_only.len(), file_only.len());
    say!("{:<50}  {:<26}  {:<30}  {:>6}  {:>9}", "run folder", "sources", "rows by source", "1 copy", "stage_s");
    runs.iter().for_each(|r| say!("{}", run_line(r)));

    say!("\nruns known by their file log only: {}", file_only.len());
    let memory_read = |boot: &str| runs.iter().any(|r| r.key == boot && r.listing.memory.is_some());
    for run in &file_only {
        let verdict = match first.get(&run.key[FILE_ONLY.len()..]).map(|&i| (i, &keeper_runs[i])) {
            None => "not in seen.tsv: its first sighting is unknown".to_string(),
            Some((0, k)) => format!("first seen by the first keeper run ({}): written before the keeper ran, it may \
                                     have reached Linux", k.utc),
            Some((_, k)) if memory_read(&k.boot) => format!("never reached a Linux boot that ran the keeper (a hang \
                                     in the firmware, or another system started); first seen {}", k.utc),
            Some((_, k)) => format!("first seen {} in a boot whose memory log was not read: whether it reached Linux \
                                     is unknown", k.utc),
        };
        say!("  {}  {verdict}", run.name);
    }

    let unattached_path = merged.join("unattached").join("firmware.log");
    let unattached = match fs::read(&unattached_path) {
        Ok(d) => parse_log(&d).map_err(|e| errors.add(&unattached_path, e)).unwrap_or_default(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Vec::new(),
        Err(e) => {
            errors.add(&unattached_path, e);
            Vec::new()
        }
    };
    records(&runs, &unattached, &merged, &mut errors);

    let gaps: Vec<String> = keeper_runs.windows(2).filter_map(|w| gap(&w[0], &w[1])).collect();
    say!("\nfile logs overwritten before a keeper run saw them:{}", if gaps.is_empty() { " none" } else { "" });
    gaps.iter().for_each(|g| say!("  {g}"));

    let long: Vec<(&Run, i64)> = runs.iter().filter_map(|r| Some((r, stage_ms(r)?)))
        .filter(|&(_, ms)| ms as f64 > stage_over * 1000.0).collect();
    say!("\nfirmware stages over {stage_over} s: {}", long.len());
    long.iter().for_each(|(r, ms)| say!("  {}  {} s", r.name, secs(*ms)));

    say!("\nlines repeated more than {repeat_over} times in one run:");
    let mut none = true;
    for run in &runs {
        let mut count = BTreeMap::<&str, usize>::new();
        run.rows.iter().filter(|r| r.source.record().is_none()).for_each(|r| *count.entry(&r.text).or_insert(0) += 1);
        let mut over: Vec<(&str, usize)> = count.into_iter().filter(|&(_, n)| n > repeat_over).collect();
        over.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
        for (text, n) in over {
            say!("  {}  {n}x  {text}", run.name);
            none = false;
        }
    }
    if none {
        say!("  none");
    }

    if let Ok(dir) = fs::read_dir(&memory) {
        let n = dir.count();
        if n > 0 {
            say!("\nstaging: {n} entries in {} not merged yet (the next keeper run merges them)", memory.display());
        }
    }
    errors.finish()
}

/// Every row of every run and of unattached/, by firmware-clock time; rows without a time come last.
fn timeline() -> i32 {
    let mut errors = Errors::default();
    let merged = root("FWLK_MERGED_ROOT", "/var/log/firmware");
    let mut all: Vec<(String, Row)> = runs(&merged, &mut errors).into_iter()
        .flat_map(|run| run.rows.into_iter().map(move |r| (run.key.clone(), r))).collect();
    let unattached = merged.join("unattached").join("firmware.log");
    if unattached.exists() {
        if let Some(rows) = read(&unattached, &mut errors).and_then(|d| parse_log(&d).map_err(|e| errors.add(&unattached, e)).ok()) {
            all.extend(rows.into_iter().map(|r| ("unattached".to_string(), r)));
        }
    }
    // a stable sort keeps each run's own order (SPEC 3.4) among rows of the same time
    all.sort_by_key(|(_, r)| (r.time.is_none(), r.time));
    say!("time\tuptime_s\tsource\trun\tmessage");
    for (key, r) in &all {
        say!("{}\t{}\t{}\t{key}\t{}", dash(r.time), dash(r.uptime.map(secs)), source_name(&r.source), r.text);
    }
    errors.finish()
}

fn decode(file: &str) -> i32 {
    let data = match fs::read(file) {
        Ok(d) => d,
        Err(e) => {
            say!("error: {file}: {e}");
            return 1;
        }
    };
    match firmware_memory_log::decode(&data) {
        Ok((lines, skipped)) => {
            say!("time\tuptime_s\tlevel\tmessage");
            for l in lines {
                say!("{}\t{}\tL{:08x}\t{}", dash(l.time), secs(l.ms), l.level, l.text);
            }
            if skipped > 0 {
                eprintln!("{skipped} bytes not decoded");
            }
            0
        }
        Err(e) => {
            say!("error: {file}: {e}");
            1
        }
    }
}

fn usage() -> ! {
    say!("{USAGE}");
    std::process::exit(2);
}

fn main() {
    let args: Vec<String> = std::env::args_os().skip(1).map(|a| a.into_string().unwrap_or_else(|_| usage()))
        .collect();
    let code = match args.iter().map(String::as_str).collect::<Vec<_>>()[..] {
        ["timeline"] => timeline(),
        ["decode-advlog", file] => decode(file),
        ref opts => {
            let (mut stage_over, mut repeat_over) = (STAGE_OVER_S, REPEAT_OVER);
            for pair in opts.chunks(2) {
                match pair {
                    ["--stage-over", v] => stage_over = v.parse().ok().filter(|s: &f64| *s >= 0.0).unwrap_or_else(|| usage()),
                    ["--repeat-over", v] => repeat_over = v.parse().unwrap_or_else(|_| usage()),
                    _ => usage(),
                }
            }
            summary(stage_over, repeat_over)
        }
    };
    std::process::exit(code);
}
