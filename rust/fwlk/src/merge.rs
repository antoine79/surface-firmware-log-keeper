//! The merge of SPEC section 3: row keys, the rows of one run's firmware.log, their order, and the check of 3.5.
use std::cmp::Ordering;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::rc::Rc;

use crate::advlog::Line;
use crate::cper::Record;
use crate::filelog::FileLog;
use crate::time::Time;
use crate::{number, Error, Res};

pub const COLUMNS: &str = "time\tuptime_s\tseg\tlevel\tsource\tmem_line\tfile_line\tmessage\n";

/// One row of a firmware.log. Its first five fields are the order of 3.4 (number is the line number, or the record
/// id of an error record, whose hash follows). A line number of 0 is none.
#[derive(Clone, Debug)]
pub struct Row {
    pub seg: u64,
    pub uptime: Option<i128>,
    pub source: String,
    pub number: u64,
    pub hash: String,
    pub time: Option<Time>,
    pub level: String,
    pub mem_line: u64,
    pub file_line: u64,
    pub message: String,
}

impl Row {
    fn order(&self, o: &Row) -> Ordering {
        (self.seg, &self.uptime, &self.source, self.number, &self.hash)
            .cmp(&(o.seg, &o.uptime, &o.source, o.number, &o.hash))
    }
}

/// The message column: trailing spaces, tabs, CR and LF removed, tabs replaced by spaces.
pub fn norm(text: &str) -> String {
    text.trim_end_matches([' ', '\t', '\r', '\n']).replace('\t', " ")
}

/// A row key of 3.3: (segment, uptime ms, message, occurrence).
pub type Key = (u64, i128, String, usize);

/// Row keys of the (uptime ms, message) lines of one source.
pub fn keys<'a>(lines: impl Iterator<Item = (i128, &'a str)>) -> Vec<Key> {
    let (mut seg, mut prev, mut count, mut out) = (0, None, HashMap::new(), Vec::new());
    for (ms, message) in lines {
        if prev.is_some_and(|p| ms < p) {
            seg += 1;
        }
        prev = Some(ms);
        let n = count.entry((seg, ms, message)).or_insert(0);
        *n += 1;
        out.push((seg, ms, message.to_string(), *n));
    }
    out
}

pub fn mem_keys(mem: Option<&[Line]>) -> Vec<Key> {
    keys(mem.unwrap_or(&[]).iter().map(|l| (l.ms, l.message.as_str())))
}

/// The text of a file-log line without the tags newer Project Mu firmware writes before it, and only there: a boot
/// phase ("[DXE  ] ", or "[0004] " for a phase it has no name for), then a debug level ("[INFO] ").
pub fn untag(message: &str) -> &str {
    // `\[` then n characters that are not `]` (or 4 hex digits) then `]`, followed by a space or the end
    fn tag(s: &str, n: usize, hex: bool) -> Option<&str> {
        let mut it = s.strip_prefix('[')?.chars();
        for _ in 0..n {
            let c = it.next()?;
            if c == ']' || hex && !c.is_ascii_hexdigit() {
                return None;
            }
        }
        let rest = it.as_str().strip_prefix(']')?;
        if rest.is_empty() { Some(rest) } else { rest.strip_prefix(' ') }
    }
    let s = tag(message, 5, false).or_else(|| tag(message, 4, true)).unwrap_or(message);
    tag(s, 4, false).unwrap_or(s)
}

pub fn file_keys(file: &FileLog, tagged: bool) -> Vec<Key> {
    keys(file.lines.iter().map(|(_, ms, m)| (*ms, if tagged { untag(m) } else { m.as_str() })))
}

/// (row keys, tagged) of a file log if every one of its keys is among in_mem, the set of a memory log's keys
/// (3.1), else None. The text is compared as it is, then, if that is not complete, without the firmware's tags.
pub fn matches(file: &FileLog, in_mem: &HashSet<Key>) -> Option<(Vec<Key>, bool)> {
    [false, true].into_iter().map(|tagged| (file_keys(file, tagged), tagged))
        .find(|(found, _)| found.iter().all(|k| in_mem.contains(k)))
}

/// Is `start` the beginning of the memory log `log`: same uptime and text, line by line? The last line of
/// `start` may be the unfinished form of its line in `log` (a dump taken before the firmware ended that line).
pub fn begins(log: &[Line], start: &[Line]) -> bool {
    let n = start.len();
    if n > log.len() {
        return false;
    }
    let same = |a: &Line, b: &Line| a.ms == b.ms && a.message == b.message;
    if n == 0 {
        return true;
    }
    let (a, b) = (&start[n - 1], &log[n - 1]);
    start[..n - 1].iter().zip(log).all(|(a, b)| same(a, b))
        && (same(a, b) || a.ms <= b.ms && b.message.starts_with(&a.message))
}

/// What render() gives: the rows in order, the hash of the file log merged, whether it was read without its tags.
pub struct Rendered {
    pub rows: Vec<Row>,
    pub merged: Option<String>,
    pub tagged: bool,
}

/// The rows of one run, in order. mem: the memory log's lines or None; files: {hash: FileLog} of every file log
/// of the run; records: {hash: (timestamp, record id, message)} of its error records.
pub fn render(mem: Option<&[Line]>, files: &BTreeMap<String, Rc<FileLog>>, records: &BTreeMap<String, Record>)
              -> Res<Rendered> {
    // the file merged: the longest of those the memory log holds, the lowest hash if several are as long; a
    // file-only run (mem None) merges its file as it is
    let in_mem = mem_keys(mem);
    let set: HashSet<Key> = in_mem.iter().cloned().collect();
    let mut best: Option<(String, Vec<Key>, bool)> = None;
    for (h, f) in files {
        let m = if mem.is_none() { Some((file_keys(f, false), false)) } else { matches(f, &set) };
        if let Some((k, tagged)) = m {
            if best.as_ref().is_none_or(|b| k.len() > b.1.len()) {
                best = Some((h.clone(), k, tagged));
            }
        }
    }
    let order: &[Key] = best.as_ref().map_or(&[], |b| &b.1);
    // key -> index of the file line
    let mut twins: HashMap<&Key, usize> = order.iter().enumerate().map(|(i, k)| (k, i)).collect();
    let file: Option<&FileLog> = best.as_ref().map(|b| &*files[&b.0]);
    let mut rows = Vec::new();
    for (n, (key, l)) in in_mem.iter().zip(mem.unwrap_or(&[])).enumerate() {
        let n = n as u64 + 1;
        let twin = twins.remove(key).and_then(|i| file.map(|f| f.lines[i].0 as u64));
        rows.push(Row { seg: key.0, uptime: Some(l.ms), source: if twin.is_some() { "both" } else { "memory" }.into(),
                        number: n, hash: String::new(), time: l.time, level: l.level.clone(), mem_line: n,
                        file_line: twin.unwrap_or(0), message: l.message.clone() });
    }
    if let Some(f) = file {
        for (i, key) in order.iter().enumerate().filter(|(_, k)| twins.contains_key(k)) {
            // a file-only run: the file's text as it is
            let (n, ms, message) = &f.lines[i];
            rows.push(Row { seg: key.0, uptime: Some(*ms), source: "bootefi".into(), number: *n as u64,
                            hash: String::new(), time: None, level: "-".into(), mem_line: 0, file_line: *n as u64,
                            message: message.clone() });
        }
    }
    rows.sort_by(Row::order);
    // reset time: the time of the first row of the memory log minus its uptime; a file-only run, whose rows are
    // all bootefi, has the END_OF_LOG clock instead and its rows are timed from it. A time outside the years 1 to
    // 9999 leaves the run without a reset time (1.2).
    let mut reset = None;
    if mem.is_some_and(|m| !m.is_empty()) {
        reset = rows[0].time.and_then(|t| t.add_ms(-rows[0].uptime?));
    } else if let (Some(f), Some(clock)) = (file, file.and_then(|f| f.end_clock)) {
        if let Some(last) = f.lines.last() {
            let r = clock.add_ms(-last.1);
            let times: Option<Vec<Time>> =
                r.and_then(|r| rows.iter().map(|x| r.add_ms(x.uptime?)).collect());
            if let Some(times) = times {
                reset = r;
                for (row, t) in rows.iter_mut().zip(times) {
                    row.time = Some(t);
                }
            }
        }
    }
    if !records.is_empty() && reset.is_none() {
        return Err(Error::value("error records in a run without firmware-clock time"));
    }
    for (h, (time, rec_id, message)) in records {
        // a record of the run's first second can be before the reset
        let (Some(time), Some(reset)) = (time, reset) else {
            return Err(Error::value(format!("error record {h} without time in a run")));
        };
        rows.push(Row { seg: 0, uptime: Some(time.since(reset)), source: "errrec".into(), number: *rec_id,
                        hash: h.clone(), time: Some(*time), level: "-".into(), mem_line: 0, file_line: 0,
                        message: message.clone() });
    }
    rows.sort_by(Row::order);
    let tagged = best.as_ref().is_some_and(|b| b.2);
    Ok(Rendered { rows, merged: best.map(|b| b.0), tagged })
}

/// (earliest, latest) firmware-clock times of a run's log lines, or None for a run without time.
pub fn span(rows: &[Row]) -> Option<(Time, Time)> {
    let times = rows.iter().filter(|r| r.source != "errrec").filter_map(|r| r.time);
    times.clone().min().zip(times.max())
}

/// The rows of unattached/firmware.log, in its order: time, record id, hash.
pub fn unattached<'a>(records: impl Iterator<Item = (&'a String, &'a Record)>) -> Vec<Row> {
    let mut rows: Vec<Row> = records.map(|(h, (time, rec_id, message))| Row {
        seg: 0, uptime: None, source: "errrec".into(), number: *rec_id, hash: h.clone(), time: *time,
        level: "-".into(), mem_line: 0, file_line: 0, message: message.clone() }).collect();
    rows.sort_by(|a, b| (fmt_time(a.time), a.number, &a.hash).cmp(&(fmt_time(b.time), b.number, &b.hash)));
    rows
}

pub fn fmt_time(t: Option<Time>) -> String {
    let Some(t) = t else { return "-".into() };
    let [y, mo, d, h, mi, s, ms] = t.fields();
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}.{ms:03}")
}

pub fn fmt_uptime(ms: Option<i128>) -> String {
    ms.map_or("-".into(), |v| format!("{}{}.{:03}", if v < 0 { "-" } else { "" }, v.unsigned_abs() / 1000, v.unsigned_abs() % 1000))
}

fn line_number(n: u64) -> String {
    if n == 0 { "-".into() } else { n.to_string() }
}

pub fn log_bytes(rows: &[Row]) -> Vec<u8> {
    let mut out = String::from(COLUMNS);
    for r in rows {
        out += &[fmt_time(r.time), fmt_uptime(r.uptime), r.seg.to_string(), r.level.clone(), r.source.clone(),
                 line_number(r.mem_line), line_number(r.file_line), r.message.clone()].join("\t");
        out.push('\n');
    }
    out.into_bytes()
}

/// `hash=([0-9a-f]{16})` searched in a text.
pub fn record_hash(text: &str) -> Option<&str> {
    text.match_indices("hash=").map(|(i, _)| &text.as_bytes()[i + 5..])
        .find(|b| b.len() >= 16 && b[..16].iter().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(c)))
        .map(|b| std::str::from_utf8(&b[..16]).unwrap_or(""))
}

/// `record_id=0x([0-9a-f]+)` searched in a text: the record id, which must fit in 64 bits; 0 without one.
fn record_id(text: &str) -> Res<u64> {
    let hex = |c: char| c.is_ascii_digit() || ('a'..='f').contains(&c);
    let Some(digits) = text.match_indices("record_id=0x").map(|(i, _)| &text[i + 12..])
        .map(|s| &s[..s.find(|c| !hex(c)).unwrap_or(s.len())]).find(|d| !d.is_empty()) else { return Ok(0) };
    u64::from_str_radix(digits, 16).map_err(|_| Error::value(format!("record id {digits} beyond 64 bits")))
}

/// `YYYY-MM-DDTHH:MM:SS.mmm` in ASCII digits, a real date and time; `-` for none.
fn time_field(t: &str) -> Res<Option<Time>> {
    if t == "-" {
        return Ok(None);
    }
    let (b, shape) = (t.as_bytes(), b"dddd-dd-ddTdd:dd:dd.ddd");
    let fits = b.len() == shape.len()
        && b.iter().zip(shape).all(|(c, s)| if *s == b'd' { c.is_ascii_digit() } else { c == s });
    let n = |a: usize, z: usize| t[a..z].parse().unwrap_or(-1);
    fits.then(|| Time::new(n(0, 4), n(5, 7), n(8, 10), n(11, 13), n(14, 16), n(17, 19), n(20, 23))).flatten()
        .map(Some).ok_or_else(|| Error::value(format!("time {t:?} is not one")))
}

/// An uptime: an optional minus sign, seconds, a dot and 3 digits of milliseconds; in all, a number of 64 bits.
fn uptime_field(up: &str) -> Res<Option<i128>> {
    if up == "-" {
        return Ok(None);
    }
    let (neg, body) = up.strip_prefix('-').map_or((false, up), |b| (true, b));
    let ms = match body.split_once('.') {
        Some((whole, frac)) if !whole.is_empty() && frac.len() == 3 => number(&format!("{whole}{frac}")),
        _ => Err(Error::value(format!("uptime {up:?} is not one"))),
    }? as i128;
    Ok(Some(if neg { -ms } else { ms }))
}

/// The rows of a firmware.log.
pub fn parse(data: &[u8]) -> Res<Vec<Row>> {
    let text = std::str::from_utf8(data).map_err(|e| Error::value(format!("not UTF-8: {e}")))?;
    if !text.starts_with(COLUMNS) || !text.ends_with('\n') {
        return Err(Error::value("not a firmware.log"));
    }
    let mut rows = Vec::new();
    let body = &text[COLUMNS.len()..];
    for line in body.split('\n').take(body.split('\n').count() - 1) {
        let f: Vec<&str> = line.split('\t').collect();
        if f.len() != 8 {
            return Err(Error::value(format!("row with {} fields", f.len())));
        }
        let (time, ms) = (time_field(f[0])?, uptime_field(f[1])?);
        let line_no = |x: &str| if x == "-" { Ok(0) } else { number(x).map(|n| n as u64) };
        let (mem_line, file_line) = (line_no(f[5])?, line_no(f[6])?);
        if mem_line != 0 && ms.is_none() {
            // a memory line without its uptime could not be compared with a dump
            return Err(Error::value(format!("memory line {mem_line} without uptime")));
        }
        let mut n = if mem_line != 0 { mem_line } else { file_line };
        let mut h = String::new();
        if f[4] == "errrec" {
            n = record_id(f[7])?;
            h = record_hash(f[7]).unwrap_or("").to_string();
        }
        rows.push(Row { seg: number(f[2])? as u64, uptime: ms, source: f[4].into(), number: n, hash: h, time,
                        level: f[3].into(), mem_line, file_line, message: f[7].into() });
    }
    Ok(rows)
}

/// The memory log's lines back from the rows of a firmware.log, in line order.
pub fn memory_lines(rows: &[Row]) -> Vec<Line> {
    let mut kept: Vec<&Row> = rows.iter().filter(|r| r.mem_line != 0).collect();
    kept.sort_by_key(|r| r.mem_line);
    kept.iter().map(|r| Line { ms: r.uptime.unwrap_or(0), time: r.time, level: r.level.clone(), message: r.message.clone() })
        .collect()
}

/// Does a candidate firmware.log hold what the log it replaces is the only copy of: every memory line (the
/// last one may have been unfinished then) and every error record?
pub fn keeps(data: &[u8], old: &[u8]) -> Res<()> {
    let (new, was) = (parse(data)?, parse(old)?);
    if !begins(&memory_lines(&new), &memory_lines(&was)) {
        return Err(Error::value("the log it would replace has memory lines it lacks"));
    }
    let have: HashSet<&String> = new.iter().filter(|r| r.source == "errrec").map(|r| &r.hash).collect();
    if let Some(lost) = was.iter().filter(|r| r.source == "errrec" && !have.contains(&r.hash)).map(|r| &r.hash).min() {
        return Err(Error::value(format!("the log it would replace has an error record it lacks ({lost})")));
    }
    Ok(())
}

/// A source compared in verify(): its rows by line number or hash, each as a text of its fields.
type Seen = BTreeMap<String, Vec<String>>;

fn differ(name: &str, got: &Seen, want: &Seen) -> Res<()> {
    if got == want { Ok(()) } else { Err(Error::value(format!("{name} rows differ from the source"))) }
}

/// The check of 3.5 on a candidate firmware.log read back: every line number of every merged source and every
/// record appears exactly once, with the same uptime and text as in the source.
///
/// tagged: the file log was paired without its tags; its rows then hold the text of the memory copy.
pub fn verify(data: &[u8], mem: Option<&[Line]>, file: Option<&FileLog>, records: &BTreeMap<String, Record>,
              tagged: bool) -> Res<()> {
    let (mut got_mem, mut got_file, mut got_rec) = (Seen::new(), Seen::new(), Seen::new());
    let up = |ms: Option<i128>| ms.map_or("None".into(), |m| m.to_string());
    for r in parse(data)? {
        if r.mem_line != 0 {
            got_mem.entry(r.mem_line.to_string()).or_default().push(format!("{}\t{}", up(r.uptime), r.message));
        }
        if r.file_line != 0 {
            got_file.entry(r.file_line.to_string()).or_default()
                .push(format!("{}\t{}\t{}", up(r.uptime), r.message, r.source == "both"));
        }
        if r.source == "errrec" {
            got_rec.entry(r.hash.clone()).or_default().push(r.message.clone());
        }
    }
    let both = mem.is_some(); // a file merged with a memory log is all in it (3.1): every one of its rows is `both`
    let want_mem: Seen = mem.unwrap_or(&[]).iter().enumerate()
        .map(|(n, l)| ((n + 1).to_string(), vec![format!("{}\t{}", l.ms, l.message)])).collect();
    let want_file: Seen = file.map_or(&[][..], |f| &f.lines).iter().map(|(n, ms, m)| {
        (n.to_string(), vec![format!("{ms}\t{}\t{both}", if tagged { untag(m) } else { m })])
    }).collect();
    let want_rec: Seen = records.iter().map(|(h, r)| (h.clone(), vec![r.2.clone()])).collect();
    differ("memory", &got_mem, &want_mem)?;
    differ("bootefi", &got_file, &want_file)?;
    differ("errrec", &got_rec, &want_rec)
}
