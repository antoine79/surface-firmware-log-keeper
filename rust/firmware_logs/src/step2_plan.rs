//! Deciding, before anything is written: which run each source belongs to (3.1), and the files of every run folder
//! touched, checked (3.5). Planning reads the archive and never writes to it.
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::io;
use std::path::PathBuf;

use crate::firmware_memory_log::MemLine;
use crate::archive_reader::{Archive, FILE_ONLY};
use crate::bad;
use crate::output::Output;
use crate::firmware_error_record::Record;
use crate::step1_collect::{Dump, SeenFile, SeenRecord, Staged};
use crate::merge_logs::{self, Key, Merge};
use crate::log_format::{format_log, parse_log, COLUMNS};
use crate::dates::Time;

/// The memory log of a run.
#[derive(Default)]
enum Memory {
    #[default]
    None,
    /// the hash of its dump, and its lines
    Held { hash: String, lines: Vec<MemLine> },
    /// listed in sources.tsv, but not all its lines can be read back from the run's log, the only place they are;
    /// `lines`: those the log does hold, if it can be read
    Lost { hash: String, lines: Option<Vec<MemLine>>, why: String },
}

/// One firmware run: what its folder holds and what this keeper run adds.
#[derive(Default)]
struct Run {
    name: Option<String>,
    memory: Memory,
    /// file log hash -> slot it was first seen in
    files: BTreeMap<String, String>,
    records: BTreeSet<String>,
    fpdt: Option<Vec<u8>>,
    esrt: Option<Vec<u8>>,
    old_log: Option<Vec<u8>>,
    /// why the folder cannot be read; it is then left as it is
    broken: Option<String>,
}

impl Run {
    fn load(archive: &Archive, key: &str) -> Run {
        let Some(folder) = archive.folders.get(key) else { return Run::default() };
        let name = &folder.name;
        let broken = |why: String| Run { name: Some(name.clone()), broken: Some(why), ..Run::default() };
        let listing = match &folder.listing {
            Ok(listing) => listing,
            Err(e) => return broken(format!("{name}/sources.tsv: {e}")),
        };
        let old_log = match archive.log(name) {
            Ok(log) => log,
            Err(e) => return broken(e.to_string()),
        };
        let memory = match &listing.memory {
            None => Memory::None,
            Some((hash, count)) => {
                // the memory log's lines exist nowhere else than in the run's log; no log at all holds no line
                let log = old_log.as_deref().filter(|l| !l.is_empty()).unwrap_or(COLUMNS.as_bytes());
                let hash = hash.clone();
                match parse_log(log).map(|rows| merge_logs::memory_lines(&rows)) {
                    Ok(lines) if lines.len() == *count => Memory::Held { hash, lines },
                    Ok(lines) => {
                        let why = format!("{name}/firmware.log: {} of its {count} memory lines", lines.len());
                        Memory::Lost { hash, lines: Some(lines), why }
                    }
                    Err(e) => Memory::Lost { hash, lines: None, why: format!("{name}/firmware.log: {e}") },
                }
            }
        };
        Run { name: Some(name.clone()), memory, files: listing.files.clone(), records: listing.records.clone(), old_log,
              ..Run::default() }
    }

    /// Take a dump of this boot, unless the memory log held has lines the dump lacks: those exist nowhere else.
    /// True if every line of the dump is in the run's memory log afterwards.
    fn offer(&mut self, dump: &Dump) -> bool {
        let take = match &self.memory {
            _ if self.broken.is_some() => return false,
            Memory::None => true,
            Memory::Held { hash, lines } => *hash == dump.hash || merge_logs::begins(&dump.lines, lines),
            // the lines the log holds are kept by the check of 3.5 against the old log
            Memory::Lost { hash, lines, .. } => {
                *hash == dump.hash || lines.as_ref().is_some_and(|l| merge_logs::begins(&dump.lines, l))
            }
        };
        if take {
            self.memory = Memory::Held { hash: dump.hash.clone(), lines: dump.lines.clone() };
            return true;
        }
        // a dump that is the beginning of the memory log held adds nothing
        matches!(&self.memory, Memory::Held { lines, .. } if merge_logs::begins(lines, &dump.lines))
    }

    fn memory_lines(&self) -> io::Result<Option<&[MemLine]>> {
        match &self.memory {
            Memory::None => Ok(None),
            Memory::Held { lines, .. } => Ok(Some(lines)),
            Memory::Lost { why, .. } => Err(bad(why)),
        }
    }

    fn is_empty(&self) -> bool {
        self.name.is_none() && matches!(self.memory, Memory::None) && self.files.is_empty() && self.fpdt.is_none()
            && self.esrt.is_none()
    }

    /// The rows in order, from the sources as they are now.
    fn render(&self, archive: &Archive) -> io::Result<Merge> {
        if let Some(why) = &self.broken {
            return Err(bad(why));
        }
        let files = self.files.keys().map(|h| archive.file(h)).collect::<io::Result<Vec<_>>>()?;
        let records = self.records.iter().map(|h| archive.record(h)).collect::<io::Result<Vec<_>>>()?;
        merge_logs::render(self.memory_lines()?, files, records)
    }

    /// The files of the run's folder, its log checked.
    fn save(&self, archive: &Archive, key: &str) -> io::Result<Save> {
        let merge = self.render(archive)?;
        let log = format_log(&merge.rows);
        self.check(&log, &merge)?;
        // a new folder is named after the first firmware-clock time, or this keeper run's time; never renamed
        let label = merge.rows.first().and_then(|r| r.time).unwrap_or(archive.cfg.now).label();
        let mut written = vec![("sources.tsv", self.sources(&merge))];
        written.extend([("fpdt-boot.txt", &self.fpdt), ("esrt.txt", &self.esrt)].into_iter()
            .filter_map(|(n, d)| Some((n, d.clone()?))));
        written.push(("firmware.log", log));
        Ok(Save {
            new: self.name.is_none(),
            name: self.name.clone().unwrap_or_else(|| format!("{label}_{key}")),
            files: written,
            records: self.records.clone(),
            mem_lines: self.memory_lines()?.map_or(0, <[_]>::len),
            file_lines: merge.merged.map_or(0, |i| merge.files[i].lines.len()),
        })
    }

    /// The check of 3.5 on the log as it will be read back, and the rule that it holds every memory line and
    /// error-record row of the log it replaces, whatever sources.tsv says.
    fn check(&self, log: &[u8], merge: &Merge) -> io::Result<()> {
        let back = parse_log(log)?;
        let file = merge.merged.map(|i| (&merge.files[i], merge.untagged));
        merge_logs::verify(&back, self.memory_lines()?, file, &merge.records)?;
        match self.old_log.as_deref().filter(|old| *old != log) {
            Some(old) => merge_logs::keeps(&back, &parse_log(old)?),
            None => Ok(()),
        }
    }

    /// sources.tsv: the memory log, the file log merged then the extra copies by hash, the records by hash.
    fn sources(&self, merge: &Merge) -> Vec<u8> {
        let mut out = String::new();
        if let Memory::Held { hash, lines } = &self.memory {
            out += &format!("memory\t{hash}\t{}\t-\t-\n", lines.len());
        }
        let mut listed: Vec<_> = self.files.iter().enumerate().collect();
        listed.sort_by_key(|(i, _)| Some(*i) != merge.merged); // stable: the extra copies stay in hash order
        for (i, (hash, slot)) in listed {
            let role = if Some(i) == merge.merged { "merged" } else { "extra" };
            out += &format!("bootefi\t{hash}\t{}\t{slot}\t{role}\n", merge.files[i].lines.len());
        }
        for hash in &self.records {
            out += &format!("errrec\t{hash}\t1\t-\t-\n");
        }
        out.into_bytes()
    }
}

/// A run folder to write: the files that may have changed, sources.tsv first so that a keeper run stopped before
/// the log is written finds what it should hold, the log last.
pub struct Save {
    pub name: String,
    pub new: bool,
    pub files: Vec<(&'static str, Vec<u8>)>,
    /// the error records whose rows the log holds
    pub records: BTreeSet<String>,
    pub mem_lines: usize,
    /// lines of the file log merged
    pub file_lines: usize,
}

/// unattached/firmware.log: the records whose rows it holds, and its new content if records were added.
pub struct Unattached {
    pub records: BTreeSet<String>,
    pub log: Option<Vec<u8>>,
}

/// What the keeper run will write and may purge.
pub struct Plan {
    /// the staging folders whose every content is in its run
    pub held: BTreeSet<PathBuf>,
    pub unattached: Option<Unattached>,
    /// run key -> its folder, or why it cannot be written
    pub runs: BTreeMap<String, io::Result<Save>>,
}

/// The runs touched by this keeper run, loaded from the archive when first touched.
struct Runs<'a> {
    archive: &'a Archive<'a>,
    map: BTreeMap<String, Run>,
}

impl Runs<'_> {
    fn get(&mut self, key: &str) -> &mut Run {
        self.map.entry(key.to_string()).or_insert_with(|| Run::load(self.archive, key))
    }
}

pub fn plan(archive: &Archive, staged: &[Staged], files: &[SeenFile], records: &[SeenRecord], out: &mut Output)
            -> Plan {
    let mut runs = Runs { archive, map: BTreeMap::new() };
    let held = assign_staging(&mut runs, staged);
    for (key, folder) in &archive.folders {
        if !folder.has_log {
            runs.get(key); // a folder an earlier keeper run left without its log: written now
        }
    }
    assign_files(&mut runs, staged, files);
    let pending = assign_records(&mut runs, records);
    let unattached = place(&mut runs, pending, out);
    let runs = runs.map.iter().filter(|(_, r)| !r.is_empty()).map(|(key, run)| (key.clone(), run.save(archive, key)))
        .collect();
    Plan { held, unattached, runs }
}

/// Each staging folder is one Linux boot: its memory log, timings and versions go to that boot's run. Returns the
/// folders whose every content the run then holds: a later folder of the same boot replaces the timings and versions
/// of an earlier one, which then stays.
fn assign_staging(runs: &mut Runs, staged: &[Staged]) -> BTreeSet<PathBuf> {
    let mut offered = Vec::new();
    for s in staged.iter().filter(|s| !s.is_empty()) {
        let run = runs.get(&s.boot);
        let dump_held = s.dump.as_ref().is_none_or(|d| run.offer(d));
        run.fpdt = s.fpdt.clone().or(run.fpdt.take());
        run.esrt = s.esrt.clone().or(run.esrt.take());
        offered.push((s, dump_held));
    }
    let kept = |mine: &Option<Vec<u8>>, run: &Option<Vec<u8>>| mine.is_none() || mine == run;
    offered.into_iter().filter(|(s, dump_held)| {
        let run = &runs.map[&s.boot];
        *dump_held && kept(&s.fpdt, &run.fpdt) && kept(&s.esrt, &run.esrt)
    }).map(|(s, _)| s.path.clone()).collect()
}

/// A file log a run folder lists stays in that run; a new one belongs to the memory log of this keeper run that
/// holds every one of its row keys (the lowest boot id if several do), else it is a run of its own.
fn assign_files(runs: &mut Runs, staged: &[Staged], files: &[SeenFile]) {
    let logged: BTreeMap<&str, HashSet<Key>> = staged.iter().filter(|s| s.dump.is_some()).map(|s| {
        let lines = runs.map.get(&s.boot).and_then(|r| r.memory_lines().ok().flatten()).unwrap_or(&[]);
        (s.boot.as_str(), merge_logs::memory_keys(lines).into_iter().collect())
    }).collect();
    for f in files {
        let owners = runs.archive.owners(|l| l.files.contains_key(&f.hash));
        if !owners.is_empty() {
            for key in &owners {
                runs.get(key); // seen again: its run is checked again
            }
            continue;
        }
        let key = logged.iter().find(|(_, keys)| merge_logs::pairing(&f.log, keys).is_some())
            .map_or_else(|| format!("{FILE_ONLY}{}", f.hash), |(boot, _)| boot.to_string());
        runs.get(&key).files.entry(f.hash.clone()).or_insert_with(|| f.slot.clone());
    }
}

/// A record a run folder lists is checked again there; the records that have a row nowhere yet are returned.
fn assign_records<'r>(runs: &mut Runs, records: &'r [SeenRecord]) -> Vec<&'r Record> {
    let mut pending: Vec<&Record> = Vec::new();
    for r in records.iter().map(|r| &r.record) {
        let owners = runs.archive.owners(|l| l.records.contains(&r.hash));
        for key in &owners {
            runs.get(key);
        }
        let unattached = runs.archive.unattached.as_ref().is_some_and(|u| u.contains_key(&r.hash));
        if owners.is_empty() && !unattached && pending.iter().all(|p| p.hash != r.hash) {
            pending.push(r);
        }
    }
    pending
}

/// Place each new record (3.1): in the run whose span holds its timestamp, the one whose span starts last if
/// several do, then the greatest key; a record inside no run goes to unattached/, and stays there.
fn place(runs: &mut Runs, pending: Vec<&Record>, out: &mut Output) -> Option<Unattached> {
    let mut unattached = runs.archive.unattached.clone();
    let mut added = false;
    if !pending.is_empty() {
        let spans = spans(runs);
        for rec in pending {
            let inside = rec.time.and_then(|t| spans.iter().filter(|(a, _, b)| a.whole_second() <= t && t <= *b).max());
            if let Some((_, key, _)) = inside {
                runs.get(key).records.insert(rec.hash.clone());
            } else if let Some(u) = &mut unattached {
                u.insert(rec.hash.clone(), rec.clone());
                added = true;
            }
        }
    }
    let unattached = unattached?;
    let log = added.then(|| {
        let log = format_log(&merge_logs::unattached_rows(unattached.values()));
        let all: Vec<Record> = unattached.values().cloned().collect();
        parse_log(&log).and_then(|back| merge_logs::verify(&back, None, None, &all)).map(|_| log)
    }).transpose();
    match log {
        Ok(log) => Some(Unattached { records: unattached.into_keys().collect(), log }),
        Err(e) => {
            out.error("errrec", format!("unattached/firmware.log: {e}"));
            None
        }
    }
}

/// (start, run key, end) of the firmware-clock span of every run, touched or not. A run that cannot be read has
/// none: its failure is reported when it is saved, or is not this keeper run's business.
fn spans(runs: &Runs) -> Vec<(Time, String, Time)> {
    let archive = runs.archive;
    let untouched = archive.folders.iter().filter(|(key, _)| !runs.map.contains_key(*key)).map(|(key, f)| {
        let rows = archive.log(&f.name).ok().flatten().and_then(|log| parse_log(&log).ok());
        (key, rows)
    });
    let touched = runs.map.iter().map(|(key, run)| (key, run.render(archive).ok().map(|m| m.rows)));
    untouched.chain(touched).filter_map(|(key, rows)| {
        let (a, b) = merge_logs::span(&rows?)?;
        Some((a, key.clone(), b))
    }).collect()
}
