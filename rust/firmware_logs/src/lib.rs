//! The keeper of SPEC.md sections 1 to 7, as a library, and the formats the report shares with it.
//!
//! A keeper run collects what the firmware left (step1_collect), reads the archive (archive_reader), plans what to
//! write and delete (step2_plan), writes (step3_write), then purges what the archive now holds (step4_purge). The
//! keeper program runs the steps in order. It stores facts and never judges the content of the logs. The modules
//! are public so that both programs can use them; they are not a stable interface.
#![forbid(unsafe_code)]

pub mod archive_reader;
pub mod dates;
pub mod firmware_error_record;
pub mod firmware_log_file;
pub mod firmware_memory_log;
pub mod firmware_timings_and_versions;
pub mod log_format;
pub mod merge_logs;
pub mod output;
pub mod safe_files;
pub mod settings;
pub mod step1_collect;
pub mod step2_plan;
pub mod step3_write;
pub mod step4_purge;

use std::io;

use sha2::{Digest, Sha256};

/// A source that is not in the format the keeper knows. Every other error is the system's.
fn bad(what: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, what.to_string())
}

/// A little-endian unsigned number.
fn le(b: &[u8]) -> u64 {
    b.iter().rev().fold(0, |v, &x| v << 8 | u64::from(x))
}

/// The first 16 hex digits of the sha256 of the data: the name of every content in the archive.
fn sha16(data: &[u8]) -> String {
    Sha256::digest(data)[..8].iter().map(|b| format!("{b:02x}")).collect()
}

/// Bytes read as Latin-1: each byte is the character of that code.
fn latin1(b: &[u8]) -> String {
    b.iter().map(|&c| char::from(c)).collect()
}

/// White space as SPEC 5 means it.
const SPACE: [char; 4] = [' ', '\t', '\r', '\n'];

/// The message column: trailing white space removed, tabs replaced by spaces.
fn norm(text: &str) -> String {
    text.trim_end_matches(SPACE).replace('\t', " ")
}

/// Each run of the characters of `set` replaced by one space, none at either end.
fn squeeze(text: &str, set: &[char]) -> String {
    text.split(set).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ")
}

/// Exactly `len` lower-case hex digits.
fn is_hex(s: &str, len: usize) -> bool {
    s.len() == len && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The numbers of `text` if it has the shape of `pattern`, where `d` stands for an ASCII digit and any other byte
/// for itself: one number per run of `d`.
fn digits(text: &[u8], pattern: &[u8]) -> Option<Vec<i64>> {
    if text.len() != pattern.len() {
        return None;
    }
    let (mut out, mut cur) = (Vec::new(), None);
    for (&c, &p) in text.iter().zip(pattern) {
        if p == b'd' && c.is_ascii_digit() {
            cur = Some(cur.unwrap_or(0) * 10 + i64::from(c - b'0'));
        } else if p == c {
            out.extend(cur.take());
        } else {
            return None;
        }
    }
    out.extend(cur);
    Some(out)
}

/// A number the keeper reads back: ASCII digits only.
fn number(text: &str) -> io::Result<u64> {
    let digits = !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
    digits.then(|| text.parse().ok()).flatten().ok_or_else(|| bad(format!("{text:?} is not a number")))
}

#[cfg(test)]
mod tests {
    #[test]
    fn shapes_and_numbers() {
        assert_eq!(crate::digits(b"12:34", b"dd:dd"), Some(vec![12, 34]));
        assert_eq!(crate::digits(b"12:3x", b"dd:dd"), None);
        assert_eq!(crate::number("0042").unwrap(), 42);
        for bad in ["", "+1", "-1", " 1", "1_0", "18446744073709551616"] {
            assert!(crate::number(bad).is_err(), "{bad:?}");
        }
    }

    /// No decoder may panic, whatever bytes it is given: random bytes, and damaged copies of valid shapes.
    #[test]
    fn decoders_never_panic() {
        let mut seed = 0x2545_f491_4f6c_dd1du64;
        let mut next = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let mut alog = b"ALOG\x05\x00\x00\x00".to_vec();
        alog.resize(0x50, 0);
        alog[12..16].copy_from_slice(&0x50u32.to_le_bytes());
        alog[20..24].copy_from_slice(&0x80u32.to_le_bytes());
        alog[28..32].copy_from_slice(&0x1000u32.to_le_bytes());
        alog.extend(b"ALMS\x00\x00\x00\x80\x01\x00\x00\x00\x00\x00\x00\x00\x05\x00hello\n\x00ALM2xx");
        alog.resize(0x80, 0);
        let mut cper = vec![0u8; 4];
        cper.extend(b"CPER");
        cper.resize(4 + 128 + 72 + 56, 0);
        cper[14] = 1;
        let shapes: [&[u8]; 5] = [
            &alog,
            &cper,
            b"00:00:01.000 : [DXE  ] [INFO] a\r\n\n=== END_OF_LOG === @ === 2026/1/2 3:04:05 ===",
            b"time\tuptime_s\tseg\tlevel\tsource\tmem_line\tfile_line\tmessage\n\
              2026-01-02T03:04:05.006\t-1.000\t0\tL80000000\terrrec\t1\t-\tx record_id=0xff hash=0123456789abcdef\n",
            b"memory\t0123456789abcdef\t3\t-\t-\nbootefi\t0123456789abcdef\t1\tUEFI_Log1.txt\tmerged\n",
        ];
        for round in 0..20000 {
            let base = shapes[round % shapes.len()];
            let random = round % 7 == 0;
            let mut data = if random { (0..next() % 300).map(|_| next() as u8).collect() } else { base.to_vec() };
            for _ in 0..next() % 6 {
                if data.is_empty() {
                    break;
                }
                let i = next() as usize % data.len();
                data[i] = next() as u8;
            }
            if next() % 3 == 0 {
                data.truncate(next() as usize % (data.len() + 1));
            }
            let _ = crate::firmware_memory_log::decode(&data);
            let _ = crate::firmware_error_record::decode(&data, "0123456789abcdef");
            let file = crate::firmware_log_file::FileLog::parse(&data);
            let _ = (crate::merge_logs::file_keys(&file, true), file.clock());
            let _ = crate::log_format::parse_log(&data).map(|rows| crate::merge_logs::memory_lines(&rows));
            let _ = crate::log_format::Listing::parse(&data);
            let text = String::from_utf8_lossy(&data);
            let _ = crate::dates::Time::parse_utc(&text);
            let _ = crate::number(&text);
        }
    }
}
