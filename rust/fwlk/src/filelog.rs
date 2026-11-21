//! File log: one UEFI_Log<n>.txt of the EFI partition (SPEC 1.2).
use crate::time::Time;
use crate::{digits, latin1, norm};

const MARK: &[u8] = b"=== END_OF_LOG ===";
const CLOCK: &[u8] = b"=== END_OF_LOG === @ === ";

/// A log line: its number as an editor shows it, uptime in ms, text.
#[derive(Clone, Debug)]
pub struct FileLine {
    pub n: u64,
    pub ms: i64,
    pub text: String,
}

/// The first `END_OF_LOG` line of a flushed log.
#[derive(Clone, Debug)]
pub struct End {
    /// the firmware clock as written, tabs as spaces, without the spaces around it
    pub written: Option<String>,
    /// content bytes after the line
    pub trailing: usize,
}

/// What a file log holds up to its first `END_OF_LOG` line; `end` is None for an incomplete log.
#[derive(Clone, Debug)]
pub struct FileLog {
    pub lines: Vec<FileLine>,
    pub end: Option<End>,
}

/// The file without the padding after its content.
pub fn content(file: &[u8]) -> &[u8] {
    let n = file.iter().rposition(|b| !b" \0\r\n".contains(b)).map_or(0, |i| i + 1);
    &file[..n]
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// `HH:MM:SS.mmm : message`, where the space after the colon may be missing before an empty message.
fn parse_line(raw: &[u8]) -> Option<(i64, &[u8])> {
    let (stamp, rest) = raw.split_at_checked(14)?;
    let [h, m, s, ms] = digits(stamp, b"dd:dd:dd.ddd :")?[..] else { return None };
    let message = if rest.is_empty() { rest } else { rest.strip_prefix(b" ")? };
    Some((((h * 60 + m) * 60 + s) * 1000 + ms, message))
}

impl FileLog {
    pub fn parse(content: &[u8]) -> FileLog {
        let mut lines = Vec::new();
        let mut end = None;
        let mut next = 0;
        let mut prev = 0;
        for (raw, n) in content.split(|&b| b == b'\n').zip(1..) {
            let start = next;
            next += raw.len() + 1;
            let raw = raw.strip_suffix(b"\r").unwrap_or(raw);
            if find(raw, MARK).is_some() {
                let written = find(raw, CLOCK).map(|p| &raw[p + CLOCK.len()..])
                    .and_then(|after| Some(latin1(&after[..find(after, b" ===")?])))
                    .map(|w| w.replace('\t', " ").trim_matches(' ').to_string()).filter(|w| !w.is_empty());
                end = Some(End { written, trailing: content.len() - (start + raw.len()) });
                break;
            }
            if raw.is_empty() {
                continue; // a blank line is not a log line, but it counts in the line numbers
            }
            // a line that does not parse keeps the uptime of the line before it, and its raw text
            let (ms, message) = parse_line(raw).unwrap_or((prev, raw));
            prev = ms;
            lines.push(FileLine { n, ms, text: norm(&latin1(message)) });
        }
        FileLog { lines, end }
    }

    /// The firmware clock at the hand-over, `Y/M/D H:MM:SS` in the `END_OF_LOG` line, if it is a date and time.
    pub fn clock(&self) -> Option<Time> {
        let (mut v, mut rest) = ([0; 6], self.end.as_ref()?.written.as_deref()?);
        for (i, sep) in ["/", "/", " ", ":", ":", ""].into_iter().enumerate() {
            let n = rest.bytes().take_while(u8::is_ascii_digit).count();
            v[i] = rest[..n].parse().ok()?;
            rest = rest[n..].strip_prefix(sep)?;
        }
        rest.is_empty().then(|| Time::new(v, 0))?
    }
}

/// The text of a file-log line without the tags newer Project Mu firmware writes before it (SPEC 3.3): a boot phase
/// (`[DXE  ] `, or `[0004] ` for a phase without a name), then a debug level (`[INFO] `).
pub fn untag(text: &str) -> &str {
    // `[`, n characters other than `]` (or 4 hex digits), `]`, then a space or the end
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
    let s = tag(text, 5, false).or_else(|| tag(text, 4, true)).unwrap_or(text);
    tag(s, 4, false).unwrap_or(s)
}
