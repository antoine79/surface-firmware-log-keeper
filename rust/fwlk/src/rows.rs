//! The two files of a run folder the keeper reads back (SPEC 2 and 3.2): firmware.log and sources.tsv.
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{Display, Write};
use std::io;

use crate::time::Time;
use crate::{bad, is_hex, number};

pub const COLUMNS: &str = "time\tuptime_s\tseg\tlevel\tsource\tmem_line\tfile_line\tmessage\n";

/// Where a row comes from, with its line numbers or its record. The derived order is the order of SPEC 3.4 after
/// segment and uptime: the source by name, then the line number, else the record id and hash.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Source {
    Bootefi { file_line: u64 },
    Both { mem_line: u64, file_line: u64 },
    Errrec { id: u64, hash: String },
    Memory { mem_line: u64 },
}

impl Source {
    pub fn mem_line(&self) -> Option<u64> {
        match *self {
            Source::Both { mem_line, .. } | Source::Memory { mem_line } => Some(mem_line),
            _ => None,
        }
    }

    pub fn file_line(&self) -> Option<u64> {
        match *self {
            Source::Both { file_line, .. } | Source::Bootefi { file_line } => Some(file_line),
            _ => None,
        }
    }

    /// The hash of an error record's row.
    pub fn record(&self) -> Option<&str> {
        match self {
            Source::Errrec { hash, .. } => Some(hash),
            _ => None,
        }
    }

    fn name(&self) -> &'static str {
        match self {
            Source::Bootefi { .. } => "bootefi",
            Source::Both { .. } => "both",
            Source::Errrec { .. } => "errrec",
            Source::Memory { .. } => "memory",
        }
    }
}

/// One row of a firmware.log. The derived order is the order of a run's log (SPEC 3.4): no two rows of a log have
/// the same first three fields.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Row {
    pub seg: u64,
    /// milliseconds since reset; None for a row of unattached/
    pub uptime: Option<i64>,
    pub source: Source,
    pub time: Option<Time>,
    pub level: Option<u32>,
    pub text: String,
}

fn dash(v: Option<impl Display>) -> String {
    v.map_or("-".into(), |v| v.to_string())
}

fn uptime(ms: i64) -> String {
    format!("{}{}.{:03}", if ms < 0 { "-" } else { "" }, ms.unsigned_abs() / 1000, ms.unsigned_abs() % 1000)
}

pub fn format_log(rows: &[Row]) -> Vec<u8> {
    let mut out = String::from(COLUMNS);
    for r in rows {
        let s = &r.source;
        let _ = writeln!(out, "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}", dash(r.time), dash(r.uptime.map(uptime)), r.seg,
                         dash(r.level.map(|l| format!("L{l:08x}"))), s.name(), dash(s.mem_line()), dash(s.file_line()),
                         r.text);
    }
    out.into_bytes()
}

fn text(data: &[u8]) -> io::Result<&str> {
    std::str::from_utf8(data).map_err(|e| bad(format!("not UTF-8: {e}")))
}

/// `-`, or a value `parse` accepts.
fn optional<T>(field: &str, parse: impl Fn(&str) -> Option<T>) -> io::Result<Option<T>> {
    match field {
        "-" => Ok(None),
        _ => parse(field).map(Some).ok_or_else(|| bad(format!("field {field:?}"))),
    }
}

fn parse_uptime(text: &str) -> Option<i64> {
    let (sign, body) = text.strip_prefix('-').map_or((1, text), |b| (-1, b));
    let (whole, frac) = body.split_once('.')?;
    let ms = i64::try_from(number(whole).ok()?).ok()?.checked_mul(1000)?.checked_add(number(frac).ok()? as i64)?;
    (frac.len() == 3).then_some(sign * ms)
}

fn parse_level(text: &str) -> Option<u32> {
    let hex = text.strip_prefix('L').filter(|h| h.len() == 8 && h.bytes().all(|b| b.is_ascii_hexdigit()))?;
    u32::from_str_radix(hex, 16).ok()
}

/// The record id and hash an error record's row gives in its text.
fn parse_record(text: &str) -> io::Result<(u64, String)> {
    let after = |tag: &'static str| text.match_indices(tag).map(move |(i, _)| &text[i + tag.len()..]);
    let hash = after("hash=").filter_map(|s| s.get(..16)).find(|h| is_hex(h, 16))
        .ok_or_else(|| bad("an error-record row without its hash"))?;
    let id = after("record_id=0x").map(|s| &s[..s.find(|c: char| !c.is_ascii_hexdigit()).unwrap_or(s.len())])
        .find(|d| !d.is_empty());
    let id = id.map_or(Ok(0), |d| u64::from_str_radix(d, 16)).map_err(|_| bad("a record id beyond 64 bits"))?;
    Ok((id, hash.to_string()))
}

fn parse_row(line: &str) -> io::Result<Row> {
    let fields: Vec<&str> = line.split('\t').collect();
    let &[time, up, seg, level, source, mem, file, text] = &fields[..] else {
        return Err(bad(format!("a row with {} fields", fields.len())));
    };
    let source = match (source, mem, file) {
        ("bootefi", "-", f) => Source::Bootefi { file_line: number(f)? },
        ("both", m, f) => Source::Both { mem_line: number(m)?, file_line: number(f)? },
        ("memory", m, "-") => Source::Memory { mem_line: number(m)? },
        ("errrec", "-", "-") => parse_record(text).map(|(id, hash)| Source::Errrec { id, hash })?,
        _ => return Err(bad(format!("source {source:?} with line numbers {mem:?} and {file:?}"))),
    };
    let row = Row {
        seg: number(seg)?,
        uptime: optional(up, parse_uptime)?,
        source,
        time: optional(time, Time::parse_row)?,
        level: optional(level, parse_level)?,
        text: text.to_string(),
    };
    if row.source.mem_line().is_some() && (row.uptime.is_none() || row.level.is_none()) {
        // a memory line that lost its uptime or level could not be compared with a dump, nor rebuilt
        return Err(bad(format!("memory line {mem} without its uptime or level")));
    }
    Ok(row)
}

pub fn parse_log(data: &[u8]) -> io::Result<Vec<Row>> {
    let body = text(data)?.strip_prefix(COLUMNS).ok_or_else(|| bad("not a firmware.log"))?;
    if !body.is_empty() && !body.ends_with('\n') {
        return Err(bad("a firmware.log cut inside its last row"));
    }
    body.split_terminator('\n').map(parse_row).collect()
}

/// What a run folder's sources.tsv lists: the memory log (hash, line count), the file logs (hash to slot), the
/// error records.
#[derive(Debug, Default)]
pub struct Listing {
    pub memory: Option<(String, usize)>,
    pub files: BTreeMap<String, String>,
    pub records: BTreeSet<String>,
}

impl Listing {
    pub fn parse(data: &[u8]) -> io::Result<Listing> {
        let mut l = Listing::default();
        for line in text(data)?.split('\n').filter(|l| !l.is_empty()) {
            match line.split('\t').collect::<Vec<_>>()[..] {
                ["memory", h, n, "-", "-"] => l.memory = Some((h.into(), number(n)? as usize)),
                ["bootefi", h, n, slot, "merged" | "extra"] if number(n).is_ok() => {
                    l.files.insert(h.into(), slot.into());
                }
                ["errrec", h, "1", "-", "-"] => {
                    l.records.insert(h.into());
                }
                _ => return Err(bad(format!("line {line:?}"))),
            }
        }
        Ok(l)
    }
}
