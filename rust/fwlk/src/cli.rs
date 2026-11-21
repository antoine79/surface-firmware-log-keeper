//! The keeper's one command (SPEC 7): a keeper run, which collects, archives, merges, checks and purges (SPEC 1
//! to 6).
//!
//! The keeper stores facts and says what it did and what failed. It never comments on the content of the logs.
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::ffi::OsString;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use crate::advlog::{self, Line};
use crate::cper::Record;
use crate::filelog::{latin1, rstrip_padding};
use crate::merge;
use crate::purge::{self, is_record_var};
use crate::store::{abspath, folder_key, list, make_dirs, read, read_or_none, read_untouched, sha16, write, Kept, Run,
                   Store, FILE_ONLY};
use crate::sysfs;
use crate::time::Time;
use crate::{number, Error, Res};

const LOCATOR: &str = "AdvLoggerLocator-4d60cfb5-f481-4a98-9c81-bff86460c43e";
const SPACE: [char; 4] = [' ', '\t', '\r', '\n'];

/// `.+_([0-9a-f]{32})` on the bytes of a name: the boot id of a staging folder (<UTC time>_<boot id>).
fn boot_of(name: &[u8]) -> Option<String> {
    let (prefix, id) = name.split_at(name.len().checked_sub(33)?);
    let id = id.strip_prefix(b"_")?;
    let hex = id.iter().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b));
    (!prefix.is_empty() && !prefix.contains(&b'\n') && hex).then(|| String::from_utf8_lossy(id).into_owned())
}

pub struct Config {
    pub memory_root: PathBuf,
    pub bootefi_root: PathBuf,
    pub merged_root: PathBuf,
    pub efivars: PathBuf,
    pub uefilogs: Option<PathBuf>,
    pub devmem: PathBuf,
    pub fpdt: PathBuf,
    pub esrt: PathBuf,
    pub boot_id: String,
    /// the UTC time of this keeper run, as written everywhere
    pub utc: String,
    pub keep_runs: i64,
    pub keep_bytes: i64,
    pub fault: Option<String>,
}

impl Config {
    pub fn new(env: &HashMap<String, OsString>) -> Res<Config> {
        // an empty variable counts as unset; a value must be UTF-8 text
        let get = |name: &str| -> Res<Option<String>> {
            env.get(name).filter(|v| !v.is_empty()).map(|v| v.clone().into_string()
                .map_err(|_| Error::value(format!("{name} is not UTF-8 text")))).transpose()
        };
        let path = |name: &str, default: &str| get(name).map(|v| PathBuf::from(v.unwrap_or_else(|| default.into())));
        let uefilogs = get("FWLK_UEFILOGS")?.map(PathBuf::from).or_else(|| {
            ["/boot/efi/UefiLogs", "/efi/UefiLogs", "/boot/UefiLogs"].iter().map(PathBuf::from).find(|d| d.is_dir())
        });
        let raw = match get("FWLK_BOOT_ID")? {
            Some(b) => b,
            None => String::from_utf8(read(Path::new("/proc/sys/kernel/random/boot_id"))?)
                .map_err(|_| Error::value("the kernel's boot id is not UTF-8"))?,
        };
        let stripped = raw.trim_matches(SPACE);
        let boot_id = stripped.replace('-', "").to_ascii_lowercase();
        // it names folders: anything else would never be found again
        if boot_id.len() != 32 || !boot_id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return Err(Error::value(format!("boot id {stripped:?} is not 32 hex digits")));
        }
        let now = match get("FWLK_NOW")? {
            Some(t) => Time::parse_utc(&t).ok_or_else(|| Error::value(format!("FWLK_NOW {t:?} is not YYYY-MM-DDTHH:MM:SSZ")))?,
            None => Time::now(),
        };
        let limit = |name: &str, default: i64| get(name)?.map_or(Ok(default), |v| number(&v));
        Ok(Config {
            memory_root: path("FWLK_MEMORY_ROOT", "/var/log/firmware_memory")?,
            bootefi_root: path("FWLK_BOOTEFI_ROOT", "/var/log/firmware_bootefilogs")?,
            merged_root: path("FWLK_MERGED_ROOT", "/var/log/firmware")?,
            efivars: path("FWLK_EFIVARS", "/sys/firmware/efi/efivars")?, uefilogs,
            devmem: path("FWLK_DEVMEM", "/dev/mem")?, fpdt: path("FWLK_SYSFS_FPDT", "/sys/firmware/acpi/fpdt/boot")?,
            esrt: path("FWLK_SYSFS_ESRT", "/sys/firmware/efi/esrt/entries")?, boot_id, utc: now.utc_stamp(),
            keep_runs: limit("FWLK_KEEP_RUNS", 300)?, keep_bytes: limit("FWLK_KEEP_BYTES", 500 * 1024 * 1024)?,
            fault: get("FWLK_FAULT")?,
        })
    }
}

/// Standard output only (the journal): what was done, what was skipped, what failed.
#[derive(Default)]
pub struct Output {
    pub failed: bool,
}

impl Output {
    pub fn say(&self, text: &str) {
        let line = text.split(SPACE).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ") + "\n";
        let mut out = std::io::stdout().lock();
        let _ = out.write_all(line.as_bytes()).and_then(|_| out.flush()); // output that cannot be written is dropped
    }
    pub fn skipped(&self, source: &str, reason: &str) {
        self.say(&format!("skipped {source}: {reason}"));
    }
    pub fn error(&mut self, step: &str, what: impl std::fmt::Display) {
        self.failed = true;
        self.say(&format!("error: {step}: {what}"));
    }
}

fn show(p: &Path) -> String {
    p.display().to_string()
}

/// One memory dump of a staging folder: its lines, hash, bytes not decoded, and the dump itself if it has such bytes.
struct Dump {
    lines: Vec<Line>,
    hash: String,
    undecoded: i128,
    raw: Option<Vec<u8>>,
}

/// What one staging folder holds: memory dumps, timings and versions of one Linux boot.
struct Source {
    path: PathBuf,
    boot: String,
    dumps: Vec<Dump>,
    /// a dump of the folder (advlog.bin.tmp) cannot be read
    unmerged: bool,
    /// every line of these dumps is in the run's memory log
    held: bool,
    fpdt: Option<Vec<u8>>,
    esrt: Option<Vec<u8>>,
}

impl Source {
    fn new(path: &Path) -> Res<Source> {
        let boot = boot_of(path.file_name().map_or(&[][..], |n| n.as_bytes())).unwrap_or_default();
        let dump = read_or_none(&path.join("advlog.bin"))?;
        // a keeper run that died between writing a dump and giving it its name: one more dump of the same boot
        let mut left = read_or_none(&path.join("advlog.bin.tmp"))?;
        let unmerged = left.as_ref().is_some_and(|l| !advlog::whole(l));
        if unmerged {
            left = None;
        }
        let mut dumps = Vec::new();
        for raw in [dump, left].into_iter().flatten() {
            let (lines, undecoded) = advlog::lines(&raw)?;
            dumps.push(Dump { lines, hash: sha16(&raw), undecoded, raw: (undecoded != 0).then_some(raw) });
        }
        Ok(Source { path: path.to_path_buf(), boot, dumps, unmerged, held: false,
                    fpdt: read_or_none(&path.join("fpdt-boot.txt"))?, esrt: read_or_none(&path.join("esrt.txt"))? })
    }
}

/// The staging folders of a root, in the order of their names' bytes. Never a run folder of the archive.
fn boot_folders(root: &Path) -> Vec<PathBuf> {
    let Ok(names) = list(root) else { return Vec::new() };
    names.iter().filter(|n| boot_of(n.as_bytes()).is_some() && n.to_str().and_then(folder_key).is_none())
        .map(|n| root.join(n)).filter(|p| p.is_dir()).collect()
}

/// The path with its symbolic links resolved for its longest beginning that exists, the rest as written (SPEC 6).
fn resolved(path: &Path) -> PathBuf {
    let (mut head, mut tail) = (abspath(path), Vec::new());
    while head.parent().is_some() {
        if let Ok(p) = std::fs::canonicalize(&head) {
            return tail.iter().rev().fold(p, |p, n| p.join(n));
        }
        tail.extend(head.file_name().map(|n| n.to_os_string()));
        head.pop();
    }
    tail.iter().rev().fold(head, |p, n| p.join(n))
}

/// Are these two folders the same one, or one inside the other?
fn nested(a: &Path, b: &Path) -> bool {
    let (a, b) = (resolved(a), resolved(b));
    a.starts_with(&b) || b.starts_with(&a)
}

/// May this read of the memory log replace the dumps an earlier keeper run of this boot left in staging? Their
/// lines are in no log yet, so only if it begins with every line of each (3.1). A dump cut short under its
/// temporary name cannot be compared, and does not count.
fn replaces(folder: &Path, data: &[u8], out: &mut Output) -> Res<bool> {
    let mut new: Option<Vec<Line>> = None;
    for name in ["advlog.bin", "advlog.bin.tmp"] {
        let Some(old) = read_or_none(&folder.join(name))? else { continue };
        if old == data || name.ends_with(".tmp") && !advlog::whole(&old) {
            continue;
        }
        if new.as_ref().is_none_or(|n| n.is_empty()) {
            new = Some(advlog::lines(data)?.0);
        }
        let staged = advlog::lines(&old)?.0;
        let new = new.as_deref().unwrap_or(&[]);
        if !merge::begins(new, &staged) {
            if !merge::begins(&staged, new) {
                // a read that is the beginning of the staged dump adds nothing
                out.error("memory", format!("{} kept: the new read lacks lines of the staged dump", show(folder)));
            }
            return Ok(false);
        }
    }
    Ok(true)
}

/// Copy this boot's memory log, timings and versions to staging, where they stay until merged and checked.
fn stage(cfg: &Config, out: &mut Output) {
    let suffix = format!("_{}", cfg.boot_id);
    let folder = boot_folders(&cfg.memory_root).into_iter().find(|p| p.as_os_str().as_bytes().ends_with(suffix.as_bytes()))
        .unwrap_or_else(|| cfg.memory_root.join(format!("{}_{}", cfg.utc, cfg.boot_id)));
    for (step, name) in [("memory", "advlog.bin"), ("fpdt", "fpdt-boot.txt"), ("esrt", "esrt.txt")] {
        let result = (|| -> Res<()> {
            let data = match step {
                "memory" => memory(cfg, out)?,
                _ => {
                    let root = if step == "fpdt" { &cfg.fpdt } else { &cfg.esrt };
                    let data = if step == "fpdt" { sysfs::fpdt(root)? } else { sysfs::esrt(root)? };
                    if data.is_none() {
                        out.skipped(step, &format!("nothing in {}", show(root)));
                    }
                    data
                }
            };
            if let Some(data) = data {
                if step != "memory" || replaces(&folder, &data, out)? {
                    make_dirs(&folder)?;
                    write(&folder.join(name), &data, None)?;
                }
            }
            Ok(())
        })();
        if let Err(e) = result {
            out.error(step, e); // one failing source never stops the others
        }
    }
}

/// This boot's memory log, read through the locator variable and /dev/mem; None when the source is missing.
fn memory(cfg: &Config, out: &mut Output) -> Res<Option<Vec<u8>>> {
    let locator = match read(&cfg.efivars.join(LOCATOR)) {
        Ok(l) => l,
        Err(e) if e.is_os() => {
            out.skipped("memory", "no AdvLoggerLocator variable");
            return Ok(None);
        }
        Err(e) => return Err(e),
    };
    match advlog::read_live(&cfg.devmem, &locator) {
        Ok(d) => Ok(Some(d)),
        Err(e) if e.is_os() => {
            // no /dev/mem, or not readable: the source is missing on this machine
            out.skipped("memory", &format!("{}: {}", show(&cfg.devmem), e.reason));
            Ok(None)
        }
        Err(e) => Err(e),
    }
}

/// Archive every file log of the EFI partition, which is only ever read, and without updating access times.
/// Returns [(hash, slot)], one per content.
fn file_logs(cfg: &Config, store: &mut Store, out: &mut Output) -> Vec<(String, String)> {
    let Some(dir) = cfg.uefilogs.as_ref().filter(|d| d.is_dir()) else {
        out.skipped("bootefi", "no UefiLogs folder");
        return Vec::new();
    };
    let mut index = "-".to_string();
    match read_untouched(&dir.join("UEFI_Index.txt")) {
        // each run of NUL, space, tab, CR and LF is one space, none at the ends
        Ok(raw) => {
            let text = latin1(&raw.unwrap_or_default());
            let words = text.split(['\0', ' ', '\t', '\r', '\n']).filter(|w| !w.is_empty()).collect::<Vec<_>>();
            if !words.is_empty() {
                index = words.join(" ");
            }
        }
        Err(e) => out.error("bootefi", format!("UEFI_Index.txt: {e}")), // the file logs are archived all the same
    }
    let mut found: Vec<(String, String)> = Vec::new();
    for slot in (1..10).map(|n| format!("UEFI_Log{n}.txt")) {
        let result = (|| -> Res<()> {
            let Some(data) = read_untouched(&dir.join(&slot))? else { return Ok(()) };
            let content = rstrip_padding(&data);
            if content.is_empty() {
                out.skipped(&slot, "no content");
                return Ok(());
            }
            let h = sha16(content);
            match store.keep_file(&h, content)? {
                Kept::Other => return Err(Error::value(format!("the archive copy {h}.txt holds other bytes"))),
                Kept::New => out.say(&format!("archived file log {h} from {slot}")),
                Kept::Same => {}
            }
            let log = store.file(&h)?;
            store.seen_file(&[&cfg.utc, &cfg.boot_id, &slot, &h, &content.len().to_string(), &log.trailing.to_string(),
                              if log.complete { "yes" } else { "no" }, &log.end_written, &index])?;
            if !found.iter().any(|(x, _)| *x == h) {
                found.push((h, slot.clone()));
            }
            Ok(())
        })();
        if let Err(e) = result {
            out.error(&slot, e);
        }
    }
    found
}

fn run<'a>(runs: &'a mut HashMap<String, Run>, store: &Store, key: &str) -> &'a mut Run {
    runs.entry(key.to_string()).or_insert_with(|| Run::new(store, key))
}

/// One keeper run.
pub fn keep(cfg: &Config, out: &mut Output) -> Res<()> {
    // 0. The three roots must be three separate folders: staging folders are removed once merged, which must never
    //    reach the archive itself. None may touch the folders of the firmware (UefiLogs, efivars).
    let roots = [&cfg.memory_root, &cfg.bootefi_root, &cfg.merged_root];
    let firmware: Vec<&PathBuf> = cfg.uefilogs.iter().chain([&cfg.efivars]).filter(|p| !p.as_os_str().is_empty())
        .collect();
    for (i, a) in roots.iter().enumerate() {
        for b in roots[i + 1..].iter().chain(firmware.iter()) {
            if nested(a, b) {
                out.error("keeper", format!("{} and {} are the same folder, or one is inside the other", show(a), show(b)));
                return Ok(());
            }
        }
    }

    let mut store = Store::new(cfg)?;
    let mut runs: HashMap<String, Run> = HashMap::new();
    let checked = |runs: &HashMap<String, Run>, key: &str| runs.get(key).is_some_and(|r| r.verified);

    // 1. Memory logs: this boot's goes to staging first; then every staging folder (those a crashed keeper run
    //    left too) is one memory log of one Linux boot. A dump never replaces a memory log that has lines it lacks.
    stage(cfg, out);
    let mut sources = Vec::new();
    for path in boot_folders(&cfg.memory_root) {
        match Source::new(&path) {
            Ok(s) => sources.push(s),
            Err(e) => out.error("memory", format!("{}: {e}", show(&path))),
        }
    }
    let truthy = |x: &Option<Vec<u8>>| x.as_ref().is_some_and(|v| !v.is_empty());
    for s in sources.iter_mut() {
        if !s.dumps.is_empty() {
            // each one is offered: a longer dump of the same boot replaces a shorter one
            let r = run(&mut runs, &store, &s.boot);
            let held: Vec<bool> = s.dumps.iter().map(|d| r.offer_memory(&d.lines, &d.hash)).collect();
            s.held = held.iter().all(|&h| h);
        }
        if truthy(&s.fpdt) || truthy(&s.esrt) {
            let r = run(&mut runs, &store, &s.boot);
            if truthy(&s.fpdt) {
                r.fpdt = s.fpdt.clone();
            }
            if truthy(&s.esrt) {
                r.esrt = s.esrt.clone();
            }
        }
    }
    let mut logged: BTreeMap<String, HashSet<merge::Key>> = BTreeMap::new();
    for s in sources.iter().filter(|s| !s.dumps.is_empty()) {
        logged.insert(s.boot.clone(), merge::mem_keys(runs[&s.boot].mem.as_deref()).into_iter().collect());
    }
    for key in store.unfinished.clone() {
        run(&mut runs, &store, &key); // a folder an earlier keeper run left without its log: written now
    }

    let (mut unattached, mut unattached_ok) = match store.load_unattached() {
        Ok(u) => (u, true),
        Err(e) => {
            // unreadable: nothing is added to it, no record depending on it is deleted
            out.error("errrec", format!("unattached/firmware.log: {e}"));
            (BTreeMap::new(), false)
        }
    };

    // 2. File logs (3.1): a file belongs to the memory log that has every one of its row keys (the lowest boot id if
    //    several do); any other file is a run of its own, unless a run folder already lists it.
    let (mut alone, mut taken) = (BTreeMap::<String, String>::new(), HashMap::<String, String>::new());
    let mut absorbed: BTreeMap<String, (String, Option<BTreeSet<String>>)> = BTreeMap::new(); // fileonly- key -> its run
    for (h, slot) in file_logs(cfg, &mut store, out) {
        let own = format!("{FILE_ONLY}{h}");
        let file = store.file(&h)?;
        let boot = logged.iter().find(|(_, keys)| merge::matches(&file, keys).is_some()).map(|(b, _)| b.clone());
        let others: BTreeSet<String> = store.file_owners.get(&h).map_or_else(BTreeSet::new, |o| {
            o.iter().filter(|k| **k != own).cloned().collect()
        });
        for key in &others {
            run(&mut runs, &store, key); // seen again: its run is checked again below
        }
        if let Some(boot) = boot.filter(|b| others.iter().all(|k| k == b)) {
            run(&mut runs, &store, &boot).files.entry(h.clone()).or_insert(slot);
            taken.insert(h, boot);
        } else if others.is_empty() {
            alone.insert(h, slot);
        }
    }
    // a fileonly- folder whose file log a boot's folder holds goes into that folder (2.1), whether the file is
    // still in UefiLogs or not
    for own in store.folders.keys().cloned().collect::<Vec<_>>() {
        let Some(h) = own.strip_prefix(FILE_ONLY) else { continue };
        if alone.contains_key(h) {
            continue;
        }
        let into = taken.get(h).cloned().or_else(|| {
            store.file_owners.get(h).and_then(|o| o.iter().find(|k| **k != own).cloned())
        });
        if let Some(into) = into {
            if run(&mut runs, &store, &into).files.contains_key(h) {
                absorbed.insert(own.clone(), (into, None));
            }
        }
    }
    for (h, slot) in &alone {
        run(&mut runs, &store, &format!("{FILE_ONLY}{h}")).files.entry(h.clone()).or_insert(slot.clone());
    }
    let mut pending: Vec<String> = Vec::new(); // hashes of the error records that need a row
    let absorbed_keys: BTreeSet<String> = absorbed.keys().cloned().collect();
    for own in &absorbed_keys {
        // what the folder holds: the records it lists and those its log has, whatever its sources.tsv says
        let r = Run::new(&store, own);
        let held = (|| -> Res<Option<BTreeSet<String>>> {
            let log = read_or_none(&store.root.join(store.folders.get(own).cloned().unwrap_or_default()).join("firmware.log"))?;
            if r.problem.is_some() {
                return Ok(None);
            }
            let rows = merge::parse(log.as_deref().filter(|l| !l.is_empty()).unwrap_or(merge::COLUMNS.as_bytes()))?;
            let mut held = r.records.clone();
            held.extend(rows.into_iter().filter(|x| x.source == "errrec").map(|x| x.hash));
            Ok(Some(held))
        })().unwrap_or(None); // not known: the folder stays
        for h in held.iter().flatten() {
            let elsewhere: Vec<String> = store.record_owners.get(h).map_or_else(Vec::new, |o| {
                o.iter().filter(|k| !absorbed_keys.contains(*k)).cloned().collect()
            });
            for key in &elsewhere {
                run(&mut runs, &store, key); // its row there is checked again before the folder goes
            }
            if elsewhere.is_empty() && !unattached.contains_key(h) {
                pending.push(h.clone());
            }
        }
        // the folder goes only once each of these records has a checked row elsewhere
        if let Some(v) = absorbed.get_mut(own) {
            v.1 = held;
        }
    }
    for key in absorbed.keys() {
        runs.remove(key);
    }

    // 3. Error records: archived raw by content; one that has no row yet is placed below.
    let mut seen: Vec<(String, String)> = Vec::new(); // (variable name, hash) of the records whose archive copy is right
    let found: Vec<String> = match list(&cfg.efivars) {
        Ok(names) => names.into_iter().filter_map(|n| n.into_string().ok()).filter(|n| is_record_var(n)).collect(),
        Err(e) if e.is_os() => {
            out.skipped("errrec", &format!("{}: {}", show(&cfg.efivars), e.reason));
            Vec::new()
        }
        Err(e) => return Err(e),
    };
    for name in &found {
        let kept = read(&cfg.efivars.join(name)).and_then(|raw| {
            let h = sha16(&raw);
            store.keep_record(&h, &raw).map(|k| (h, k))
        });
        let (h, new) = match kept {
            Ok(x) => x,
            Err(e) => {
                out.error("errrec", format!("{name}: {e}"));
                continue;
            }
        };
        if let Kept::Other = new {
            store.seen_record("kept", name, &h, "archive-mismatch");
            out.error("errrec", format!("{name} kept: the archive copy {h}.bin holds other bytes"));
            continue;
        }
        if let Kept::New = new {
            let failed = store.seen_record("archived", name, &h, "-");
            out.say(&format!("archived error record {h} from {name}"));
            if let Some(f) = failed {
                out.error("errrec", format!("{name}: seen.tsv: {f}"));
            }
        }
        let owners: Vec<String> = store.record_owners.get(&h).map_or_else(Vec::new, |o| o.iter().cloned().collect());
        for key in owners.iter().filter(|k| !absorbed.contains_key(*k)) {
            run(&mut runs, &store, key);
        }
        if owners.is_empty() && !unattached.contains_key(&h) {
            pending.push(h.clone());
        }
        seen.push((name.clone(), h));
    }

    // 4. Place the records (3.1): the run whose span holds the timestamp; if several, the one whose span starts
    //    last, then the greatest key. The pending ones are compared with every run. A row of unattached/ was
    //    compared with every run when it was written, so only the runs touched since can take it.
    let mut moving: BTreeMap<String, String> = BTreeMap::new(); // hash of a row of unattached/ -> run that has it now
    for h in unattached.keys() {
        let first = store.record_owners.get(h).and_then(|o| o.iter().find(|k| !absorbed.contains_key(*k)).cloned());
        if let Some(key) = first {
            run(&mut runs, &store, &key); // a keeper run died between writing it there and removing it here
            moving.insert(h.clone(), key);
        }
    }
    let mut once = HashSet::new();
    pending.retain(|h| once.insert(h.clone()));
    let waiting: Vec<String> = unattached.iter()
        .filter(|(h, r)| !moving.contains_key(*h) && !pending.contains(h) && r.0.is_some()).map(|(h, _)| h.clone()).collect();
    if !pending.is_empty() || !waiting.is_empty() {
        let mut keys: BTreeSet<String> = runs.keys().cloned().collect();
        if !pending.is_empty() {
            keys.extend(store.folders.keys().filter(|k| !absorbed.contains_key(*k)).cloned());
        }
        let mut spans: BTreeMap<String, Option<(Time, Time)>> = BTreeMap::new();
        for key in keys {
            let rows = match runs.get(&key) {
                Some(r) => r.render(&mut store).map(|x| x.out.rows),
                None => store.folders.get(&key).cloned().map_or(Ok(Vec::new()), |name| {
                    read(&store.root.join(name).join("firmware.log")).and_then(|d| merge::parse(&d))
                }),
            };
            // a failure is reported when that run is saved, or is not this keeper run's business
            spans.insert(key, rows.ok().and_then(|r| merge::span(&r)));
        }
        let before = unattached.clone();
        for h in pending.iter().chain(waiting.iter()) {
            let placed = (|| -> Res<()> {
                let time = if pending.contains(h) { store.record(h)?.0 } else { unattached.get(h).and_then(|r| r.0) };
                let inside = spans.iter().filter_map(|(key, span)| {
                    let (a, b) = (*span)?;
                    let t = time?;
                    (a.whole_second() <= t && t <= b).then_some((a, key.clone()))
                }).max();
                if let Some((_, key)) = inside {
                    store.record(h)?; // the run's log is made from the archive copy: it must be readable
                    run(&mut runs, &store, &key).records.insert(h.clone());
                    if unattached.contains_key(h) {
                        moving.insert(h.clone(), key);
                    }
                } else if unattached_ok && pending.contains(h) {
                    let rec: Record = store.record(h)?;
                    unattached.insert(h.clone(), rec);
                }
                Ok(())
            })();
            if let Err(e) = placed {
                out.error("errrec", format!("{h}: {e}"));
            }
        }
        if unattached != before {
            if let Err(e) = store.save_unattached(&unattached) {
                out.error("errrec", format!("unattached/firmware.log: {e}"));
                unattached_ok = false;
            }
        }
    }

    // 5. Write the runs touched; each log replaces the one before only if it passes the check of 3.5. A new run is
    //    noted in runs.tsv when it is saved: the boots of the staging folders first, in the order of their folder
    //    names (the UTC time they were collected at), then the other runs by key.
    let mut order: Vec<String> = sources.iter().filter(|s| runs.contains_key(&s.boot)).map(|s| s.boot.clone()).collect();
    order.extend(runs.keys().cloned().collect::<BTreeSet<_>>());
    let mut done = HashSet::new();
    for key in order {
        if !done.insert(key.clone()) {
            continue;
        }
        let Some(r) = runs.get_mut(&key) else { continue };
        if r.empty() {
            continue;
        }
        match r.save(&mut store) {
            Ok((changed, mem, file, records)) => {
                if changed {
                    out.say(&format!("merged run {key} into {} (memory {mem} lines, bootefi {file} lines, errrec {records})",
                                     r.name.as_deref().unwrap_or("")));
                }
            }
            Err(e) => out.error("merge", format!("{key}: {e}")),
        }
    }
    let moved: Vec<String> = moving.iter()
        .filter(|(h, key)| checked(&runs, key) && runs[*key].records.contains(*h)).map(|(h, _)| h.clone()).collect();
    if !moved.is_empty() && unattached_ok {
        // their rows are checked in a run's log: they leave unattached/
        let mut rest = unattached.clone();
        for h in &moved {
            rest.remove(h);
        }
        match store.save_unattached(&rest) {
            Ok(()) => unattached = rest,
            Err(e) => out.error("errrec", format!("unattached/firmware.log: {e}")),
        }
    }

    // 6. Purges (4), each one only for what is written, read back and checked.
    let remove = |out: &mut Output, path: &Path| {
        if let Err(e) = purge::remove_folder(path) {
            out.error("purge", e);
        }
    };
    let has_row = |runs: &HashMap<String, Run>, h: &str| {
        runs.values().any(|r| r.verified && r.records.contains(h)) || unattached_ok && unattached.contains_key(h)
    };
    for path in store.leftovers()? {
        remove(out, &path); // run folders an earlier keeper run could not finish removing
    }
    for (key, (into, held)) in &absorbed {
        // before the staging folders: a failure here can be done again
        let name = store.folders.get(key).cloned().unwrap_or_default();
        match held {
            None => out.error("purge", format!("{name} kept: its sources.tsv or its log cannot be read")),
            Some(_) if !checked(&runs, into) => {}
            Some(held) if held.iter().all(|h| has_row(&runs, h)) => {
                store.folders.remove(key);
                if let Err(e) = store.remove_run(&name) {
                    if !e.is_os() {
                        return Err(e);
                    }
                    out.error("purge", e);
                }
            }
            Some(_) => out.error("purge", format!("{name} kept: a record of it has no checked row in another log")),
        }
    }
    for s in &sources {
        let path = show(&s.path);
        if s.unmerged {
            // with or without timings and versions beside it
            out.error("memory", format!("{path} kept: advlog.bin.tmp is not a whole dump, not merged"));
        } else if s.dumps.is_empty() && !truthy(&s.fpdt) && !truthy(&s.esrt) {
            if list(&s.path)?.is_empty() {
                remove(out, &s.path); // an empty staging folder, left by a keeper run that died at once
            } else {
                out.error("memory", format!("{path} kept: nothing readable in it"));
            }
        } else if checked(&runs, &s.boot) && (s.dumps.is_empty() || s.held) {
            if keep_raw(s, &runs, &store, out) {
                remove(out, &s.path);
            }
        } else if checked(&runs, &s.boot) {
            out.error("memory", format!("{path} kept: boot {} already has a memory log with other lines", s.boot));
        }
    }
    for (name, h) in &seen {
        if !has_row(&runs, h) {
            store.seen_record("kept", name, h, "not-verified");
            out.error("errrec", format!("{name} kept: its row is not checked in a log"));
            continue;
        }
        match purge::delete_variable(&cfg.efivars, name) {
            Err(e) if e.is_os() => {
                store.seen_record("kept", name, h, "delete-failed");
                out.error("errrec", format!("{name} kept: {e}"));
                continue;
            }
            r => r?,
        }
        let failed = store.seen_record("deleted", name, h, "-");
        out.say(&format!("deleted error record {name}"));
        if let Some(f) = failed {
            out.error("errrec", format!("{name}: seen.tsv: {f}"));
        }
    }

    // never a run written or seen again in this keeper run (its sources may be gone, or still in UefiLogs), never a
    // fileonly- folder this keeper run decided to keep
    let protected: HashSet<String> = runs.keys().chain(absorbed.keys()).cloned().collect();
    match store.prune(&protected) {
        Err(e) if e.is_os() => out.error("retention", e),
        r => r?,
    }
    Ok(())
}

/// Copy the dumps of a staging folder that have bytes the decoder could not read into the run folder: the dump is
/// the only copy of those bytes. False if one could not be copied.
fn keep_raw(s: &Source, runs: &HashMap<String, Run>, store: &Store, out: &mut Output) -> bool {
    let folder = runs[&s.boot].name.clone().unwrap_or_default();
    for d in s.dumps.iter().filter(|d| d.undecoded != 0) {
        let name = format!("advlog-{}.bin", d.hash);
        let kept = store.keep_copy(&store.root.join(&folder), &name, d.raw.as_deref().unwrap_or(&[]))
            .and_then(|k| match k {
                Kept::Other => Err(Error::os(format!("{folder}/{name} holds other bytes"))),
                _ => Ok(()),
            });
        if let Err(e) = kept {
            out.error("memory", format!("{} kept: {e}", show(&s.path)));
            return false;
        }
        out.error("memory", format!("{}: {} bytes not decoded, dump kept as {folder}/{name}", show(&s.path), d.undecoded));
    }
    true
}

pub fn main(argv: Vec<OsString>) -> i32 {
    let mut out = Output::default();
    if !argv.is_empty() {
        out.say("usage: firmware-log-keeper");
        return 2;
    }
    let env: HashMap<String, OsString> = std::env::vars_os()
        .filter_map(|(k, v)| k.into_string().ok().map(|k| (k, v))).collect();
    if let Err(e) = Config::new(&env).and_then(|cfg| keep(&cfg, &mut out)) {
        out.error("keeper", e); // nothing goes to standard error
    }
    if out.failed { 1 } else { 0 }
}
