//! File log: one UEFI_Log<n>.txt of the EFI partition (SPEC 1.2). Read only, always.
use crate::merge::norm;
use crate::time::Time;

/// Bytes read as Latin-1: each byte is the character of that code.
pub fn latin1(b: &[u8]) -> String {
    b.iter().map(|&c| c as char).collect()
}

/// What pads a file log after its content.
pub const PADDING: &[u8] = b" \x00\r\n";
const MARK: &[u8] = b"=== END_OF_LOG ===";
const END: &[u8] = b"=== END_OF_LOG === @ === ";

pub fn rstrip_padding(data: &[u8]) -> &[u8] {
    let n = data.iter().rposition(|b| !PADDING.contains(b)).map_or(0, |i| i + 1);
    &data[..n]
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// `(\d\d):(\d\d):(\d\d)\.(\d{3}) :(?: (.*))?` (bytes): (uptime in ms, message).
fn parse_line(raw: &[u8]) -> Option<(i128, &[u8])> {
    let shape = b"dd:dd:dd.ddd :";
    if raw.len() < shape.len() {
        return None;
    }
    for (c, s) in raw.iter().zip(shape) {
        if (*s == b'd') != c.is_ascii_digit() || *s != b'd' && c != s {
            return None;
        }
    }
    let n = |a: usize, b: usize| raw[a..b].iter().fold(0i128, |v, c| v * 10 + (c - b'0') as i128);
    let rest = &raw[shape.len()..];
    let message = if rest.is_empty() { rest } else { rest.strip_prefix(b" ")? };
    Some((((n(0, 2) * 60 + n(3, 5)) * 60 + n(6, 8)) * 1000 + n(9, 12), message))
}

/// `(\d+)/(\d+)/(\d+) (\d+):(\d+):(\d+)` on the END_OF_LOG clock: the time, if it is one.
fn clock(text: &str) -> Option<Time> {
    let mut v = [0i64; 6];
    let mut s = text;
    for (k, sep) in ["/", "/", " ", ":", ":", ""].iter().enumerate() {
        let n = s.bytes().take_while(u8::is_ascii_digit).count();
        if n == 0 || !s[n..].starts_with(sep) {
            return None;
        }
        v[k] = s[..n].parse().ok()?; // a number too large for any field is no date
        s = &s[n + sep.len()..];
    }
    if !s.is_empty() {
        return None;
    }
    Time::new(v[0], v[1], v[2], v[3], v[4], v[5], 0)
}

/// lines: (line number, uptime in ms, message) of every log line before the first END_OF_LOG line.
#[derive(Debug, Clone)]
pub struct FileLog {
    pub lines: Vec<(usize, i128, String)>,
    pub complete: bool,
    pub end_written: String,
    pub end_clock: Option<Time>,
    pub trailing: usize,
}

impl FileLog {
    pub fn new(content: &[u8]) -> FileLog {
        let mut f = FileLog { lines: Vec::new(), complete: false, end_written: "-".into(), end_clock: None, trailing: 0 };
        let (mut prev, mut pos) = (0, 0);
        for (n, raw) in content.split(|&b| b == b'\n').enumerate() {
            let mut end = pos + raw.len();
            pos = end + 1;
            let raw = match raw.strip_suffix(b"\r") {
                Some(r) => {
                    end -= 1;
                    r
                }
                None => raw,
            };
            if find(raw, MARK).is_some() {
                f.complete = true;
                f.trailing = content.len() - end;
                if let Some(p) = find(raw, END) {
                    let at = p + END.len();
                    if let Some(q) = find(&raw[at..], b" ===") {
                        let written = latin1(&raw[at..at + q]).replace('\t', " ");
                        let written = written.trim_matches(' ');
                        f.end_written = if written.is_empty() { "-".into() } else { written.to_string() };
                        f.end_clock = clock(&f.end_written);
                    }
                }
                break;
            }
            if raw.is_empty() {
                continue; // a blank line is not a log line, but it counts in the line numbers
            }
            // a line that does not parse keeps the uptime of the line before it and its raw text
            let message = match parse_line(raw) {
                Some((ms, message)) => {
                    prev = ms;
                    message
                }
                None => raw,
            };
            f.lines.push((n + 1, prev, norm(&latin1(message))));
        }
        f
    }
}
