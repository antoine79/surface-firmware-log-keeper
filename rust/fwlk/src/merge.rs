//! The merge of SPEC section 3, on plain data: row keys, pairing, the rows of one run, and the checks of 3.5.
use std::cmp::Reverse;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::io;

use crate::advlog::MemLine;
use crate::bad;
use crate::cper::Record;
use crate::filelog::{untag, FileLog};
use crate::rows::{Row, Source};
use crate::time::Time;

/// A row key of 3.3: (segment, uptime ms, text, occurrence).
pub type Key = (u64, i64, String, u32);

fn keys<'a>(lines: impl IntoIterator<Item = (i64, &'a str)>) -> Vec<Key> {
    let mut seg = 0;
    let mut prev = None;
    let mut seen = HashMap::new();
    lines.into_iter().map(|(ms, text)| {
        if prev.is_some_and(|p| ms < p) {
            seg += 1;
        }
        prev = Some(ms);
        let n = seen.entry((seg, ms, text)).or_insert(0);
        *n += 1;
        (seg, ms, text.to_string(), *n)
    }).collect()
}

pub fn memory_keys(lines: &[MemLine]) -> Vec<Key> {
    keys(lines.iter().map(|l| (l.ms, l.text.as_str())))
}

pub fn file_keys(file: &FileLog, untagged: bool) -> Vec<Key> {
    keys(file.lines.iter().map(|l| (l.ms, if untagged { untag(&l.text) } else { l.text.as_str() })))
}

/// (keys, untagged) of a file log whose every key is among a memory log's (3.1): compared as it is, else without
/// the firmware's tags. None if neither is complete.
pub fn pairing(file: &FileLog, memory: &HashSet<Key>) -> Option<(Vec<Key>, bool)> {
    [false, true].into_iter().map(|untagged| (file_keys(file, untagged), untagged))
        .find(|(keys, _)| keys.iter().all(|k| memory.contains(k)))
}

/// Is `start` the beginning of `log`, line by line? Its last line may be the unfinished form of its line in `log`.
pub fn begins(log: &[MemLine], start: &[MemLine]) -> bool {
    let Some((last, head)) = start.split_last() else { return true };
    let same = |a: &MemLine, b: &MemLine| a.ms == b.ms && a.text == b.text;
    log.len() >= start.len() && head.iter().zip(log).all(|(a, b)| same(a, b)) && {
        let full = &log[head.len()];
        same(last, full) || last.ms <= full.ms && full.text.starts_with(&last.text)
    }
}

/// The rows of one run, the file logs and records they come from, and which file log was merged (an index into
/// `files`) and whether without its tags.
pub struct Merge {
    pub rows: Vec<Row>,
    pub files: Vec<FileLog>,
    pub records: Vec<Record>,
    pub merged: Option<usize>,
    pub untagged: bool,
}

/// The rows of one run, in order (3.2 to 3.4). `files` are in hash order; `mem` is None for a file-only run.
pub fn render(mem: Option<&[MemLine]>, files: Vec<FileLog>, records: Vec<Record>) -> io::Result<Merge> {
    let mem_keys = memory_keys(mem.unwrap_or(&[]));
    let in_memory: HashSet<Key> = mem_keys.iter().cloned().collect();
    // the longest file the memory log holds, the lowest hash if several are as long; a file-only run takes its file
    let best = files.iter().enumerate()
        .filter_map(|(i, f)| if mem.is_some() { pairing(f, &in_memory) } else { Some((file_keys(f, false), false)) }
            .map(|(keys, untagged)| (i, keys, untagged)))
        .max_by_key(|(i, keys, _)| (keys.len(), Reverse(*i)));
    let mut rows = Vec::new();
    match (mem, &best) {
        (Some(lines), _) => {
            let twin: HashMap<&Key, u64> = best.iter()
                .flat_map(|(i, keys, _)| keys.iter().zip(&files[*i].lines).map(|(k, l)| (k, l.n))).collect();
            for ((key, l), mem_line) in mem_keys.iter().zip(lines).zip(1..) {
                let source = match twin.get(key) {
                    Some(&file_line) => Source::Both { mem_line, file_line },
                    None => Source::Memory { mem_line },
                };
                rows.push(Row { seg: key.0, uptime: Some(l.ms), source, time: l.time, level: Some(l.level),
                                text: l.text.clone() });
            }
        }
        (None, Some((i, keys, _))) => {
            for (key, l) in keys.iter().zip(&files[*i].lines) {
                rows.push(Row { seg: key.0, uptime: Some(l.ms), source: Source::Bootefi { file_line: l.n }, time: None,
                                level: None, text: l.text.clone() });
            }
        }
        (None, None) => {}
    }
    let reset = match mem {
        // the first row's time minus its uptime
        Some(_) => rows.iter().min().and_then(|r| r.time?.add_ms(-r.uptime?)),
        // the END_OF_LOG clock minus the uptime of the last line; no time at all if a row would leave the calendar
        None => best.as_ref().and_then(|(i, _, _)| file_times(&files[*i], &mut rows)),
    };
    for rec in &records {
        let (Some(time), Some(reset)) = (rec.time, reset) else {
            return Err(bad(format!("error record {} in a run without firmware-clock time", rec.hash)));
        };
        rows.push(Row { seg: 0, uptime: Some(time.since(reset)), source: errrec(rec), time: Some(time), level: None,
                        text: rec.text.clone() });
    }
    rows.sort();
    let (merged, untagged) = (best.as_ref().map(|b| b.0), best.is_some_and(|b| b.2));
    Ok(Merge { rows, files, records, merged, untagged })
}

/// The source of an error record's row.
fn errrec(rec: &Record) -> Source {
    Source::Errrec { id: rec.id, hash: rec.hash.clone() }
}

/// Give the rows of a file-only run their times (SPEC 1.2); the reset time, if there is one.
fn file_times(file: &FileLog, rows: &mut [Row]) -> Option<Time> {
    let reset = file.clock()?.add_ms(-file.lines.last()?.ms)?;
    let times: Option<Vec<Time>> = rows.iter().map(|r| reset.add_ms(r.uptime?)).collect();
    for (row, t) in rows.iter_mut().zip(times?) {
        row.time = Some(t);
    }
    Some(reset)
}

/// The rows of unattached/firmware.log, in its order: time, record id, hash.
pub fn unattached_rows<'a>(records: impl IntoIterator<Item = &'a Record>) -> Vec<Row> {
    let mut rows: Vec<Row> = records.into_iter().map(|r| Row {
        seg: 0, uptime: None, source: errrec(r), time: r.time, level: None, text: r.text.clone(),
    }).collect();
    rows.sort_by(|a, b| (a.time, &a.source).cmp(&(b.time, &b.source)));
    rows
}

/// (earliest, latest) firmware-clock times of a run's log lines.
pub fn span(rows: &[Row]) -> Option<(Time, Time)> {
    let times = rows.iter().filter(|r| r.source.record().is_none()).filter_map(|r| r.time);
    times.clone().min().zip(times.max())
}

/// The memory log's lines back from the rows of a firmware.log, in line order.
pub fn memory_lines(rows: &[Row]) -> Vec<MemLine> {
    let mut lines: Vec<(u64, MemLine)> = rows.iter().filter_map(|r| {
        Some((r.source.mem_line()?, MemLine { ms: r.uptime?, time: r.time, level: r.level?, text: r.text.clone() }))
    }).collect();
    lines.sort_by_key(|(n, _)| *n);
    lines.into_iter().map(|(_, l)| l).collect()
}

/// The check of 3.5 on the rows read back: every line of every merged source and every record appears exactly
/// once, with the same uptime and text as in the source. `file`: the file log merged, and whether without its tags.
pub fn verify(rows: &[Row], mem: Option<&[MemLine]>, file: Option<(&FileLog, bool)>, records: &[Record])
              -> io::Result<()> {
    fn same<T: Ord>(what: &str, mut got: Vec<T>, mut want: Vec<T>) -> io::Result<()> {
        got.sort();
        want.sort();
        if got == want { Ok(()) } else { Err(bad(format!("the {what} rows differ from the source"))) }
    }
    same("memory", rows.iter().filter_map(|r| Some((r.source.mem_line()?, r.uptime, r.text.as_str()))).collect(),
         mem.unwrap_or(&[]).iter().zip(1..).map(|(l, n)| (n, Some(l.ms), l.text.as_str())).collect())?;
    let (both, untagged) = (mem.is_some(), file.is_some_and(|f| f.1));
    let is_both = |r: &Row| matches!(r.source, Source::Both { .. });
    same("bootefi", rows.iter().filter_map(|r| Some((r.source.file_line()?, r.uptime, r.text.as_str(), is_both(r))))
             .collect(),
         file.map_or(&[][..], |f| &f.0.lines).iter()
             .map(|l| (l.n, Some(l.ms), if untagged { untag(&l.text) } else { l.text.as_str() }, both)).collect())?;
    same("errrec", rows.iter().filter_map(|r| Some((r.source.record()?, r.text.as_str()))).collect(),
         records.iter().map(|r| (r.hash.as_str(), r.text.as_str())).collect())
}

/// May `new` replace `old`? Only if it holds what `old` is the only copy of: every memory line (the last one may
/// have been unfinished then) and every error-record row.
pub fn keeps(new: &[Row], old: &[Row]) -> io::Result<()> {
    if !begins(&memory_lines(new), &memory_lines(old)) {
        return Err(bad("the log it would replace has memory lines it lacks"));
    }
    let hashes = |rows: &[Row]| -> BTreeSet<String> {
        rows.iter().filter_map(|r| r.source.record().map(String::from)).collect()
    };
    match hashes(old).difference(&hashes(new)).next() {
        Some(lost) => Err(bad(format!("the log it would replace has an error record it lacks ({lost})"))),
        None => Ok(()),
    }
}
