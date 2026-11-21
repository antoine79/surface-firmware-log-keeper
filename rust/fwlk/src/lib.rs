//! firmware-log-keeper, the Rust keeper of SPEC.md sections 1 to 7. Modules as in the Python reference.
#![forbid(unsafe_code)]

pub mod advlog;
pub mod cli;
pub mod cper;
pub mod filelog;
pub mod merge;
pub mod purge;
pub mod store;
pub mod sysfs;
pub mod time;

use std::fmt;
use std::io;
use std::path::Path;

/// A failure of a step. `kind` is set for a failure of the system (a file that cannot be read, written or
/// removed): some steps skip a missing source then, where any other failure is an error.
#[derive(Debug, Clone)]
pub struct Error {
    pub kind: Option<io::ErrorKind>,
    /// what failed, without the path: for a failure of the system, the system's own words
    pub reason: String,
    pub path: Option<String>,
}

pub type Res<T> = Result<T, Error>;

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match &self.path {
            Some(p) => write!(f, "{p}: {}", self.reason),
            None => f.write_str(&self.reason),
        }
    }
}

impl Error {
    /// A source that is not what the keeper expects.
    pub fn value(reason: impl Into<String>) -> Error {
        Error { kind: None, reason: reason.into(), path: None }
    }
    /// A failure of the system found by the keeper itself.
    pub fn os(reason: impl Into<String>) -> Error {
        Error { kind: Some(io::ErrorKind::Other), reason: reason.into(), path: None }
    }
    pub fn io(e: &io::Error, path: &Path) -> Error {
        let text = e.to_string();
        let reason = match e.raw_os_error() {
            Some(code) => text.strip_suffix(&format!(" (os error {code})")).unwrap_or(&text).to_string(),
            None => text,
        };
        Error { kind: Some(e.kind()), reason, path: Some(path.display().to_string()) }
    }
    pub fn is_os(&self) -> bool {
        self.kind.is_some()
    }
    pub fn is_not_found(&self) -> bool {
        self.kind == Some(io::ErrorKind::NotFound)
    }
}

/// A little-endian unsigned number.
pub fn le(b: &[u8]) -> u64 {
    b.iter().rev().fold(0, |v, &x| v << 8 | x as u64)
}

/// A number the keeper reads (SPEC 2): ASCII digits only, and below 2**63.
pub fn number(text: &str) -> Res<i64> {
    let digits = !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
    digits.then(|| text.parse().ok()).flatten().ok_or_else(|| Error::value(format!("{text:?} is not a number of 64 bits")))
}

#[cfg(test)]
mod tests {
    #[test]
    fn numbers() {
        assert_eq!(crate::number("0042").unwrap(), 42);
        assert_eq!(crate::number(&i64::MAX.to_string()).unwrap(), i64::MAX);
        for bad in ["", "+1", "-1", " 1", "1 ", "1_0", "\u{663}", "9223372036854775808"] {
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
        alog.extend([0u8; 0x50 - 8]);
        alog[12..16].copy_from_slice(&0x50u32.to_le_bytes());
        alog[20..24].copy_from_slice(&0x80u32.to_le_bytes());
        alog[28..32].copy_from_slice(&0x1000u32.to_le_bytes());
        alog.extend(b"ALMS\x00\x00\x00\x80\x01\x00\x00\x00\x00\x00\x00\x00\x05\x00hello\n\x00ALM2xx");
        alog.resize(0x80, 0);
        let mut cper = vec![0u8; 4];
        cper.extend(b"CPER");
        cper.resize(4 + 128 + 72 + 56, 0);
        cper[14] = 1;
        let shapes: [&[u8]; 4] = [&alog, &cper, b"00:00:01.000 : a\r\n\n=== END_OF_LOG === @ === 2026/1/2 3:04:05 ===",
                                  b"time\tuptime_s\tseg\tlevel\tsource\tmem_line\tfile_line\tmessage\n\
                                    2026-01-02T03:04:05.006\t-1.000\t0\t-\terrrec\t-\t-\tx record_id=0xff\n"];
        for round in 0..20000 {
            let base = shapes[round % 4];
            let mut data = if round % 5 == 0 { (0..next() % 300).map(|_| next() as u8).collect() } else { base.to_vec() };
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
            let _ = crate::advlog::lines(&data);
            let _ = crate::advlog::whole(&data);
            let _ = crate::cper::decode(&data, "0123456789abcdef");
            let f = crate::filelog::FileLog::new(&data);
            let _ = crate::merge::file_keys(&f, true);
            let _ = crate::merge::parse(&data);
            let text = String::from_utf8_lossy(&data);
            let _ = crate::time::Time::parse_utc(&text);
            let _ = crate::number(&text);
        }
    }
}
