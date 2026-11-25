//! Memory log: the Project Mu Advanced Logger buffer (SPEC 1.1).
//!
//! Layout from mu_plus AdvLoggerPkg/Include/AdvancedLoggerInternal.h.
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

use crate::safe_files::at;
use crate::dates::Time;
use crate::{bad, latin1, le, norm};

/// One log line: uptime in ms, firmware-clock time, debug level, text.
#[derive(Clone, Debug, PartialEq)]
pub struct MemLine {
    pub ms: i64,
    pub time: Option<Time>,
    pub level: u32,
    pub text: String,
}

fn field(buf: &[u8], off: usize, len: usize) -> io::Result<u64> {
    buf.get(off..off + len).map(le).ok_or_else(|| bad(format!("{} bytes, too short for a field at {off}", buf.len())))
}

/// (start, end) of the entries, as offsets from the header. `addr` is the header's physical address, if known.
fn bounds(buf: &[u8], addr: Option<u64>) -> io::Result<(usize, usize)> {
    if !buf.starts_with(b"ALOG") {
        return Err(bad("no ALOG signature"));
    }
    let (p_start, p_cur, size) = (field(buf, 8, 8)?, field(buf, 16, 8)?, field(buf, 28, 4)?);
    // header version 3 holds physical addresses at 8 and 16; newer ones hold 32-bit offsets at 12 and 20
    let v3 = addr.filter(|a| p_start.checked_sub(*a).is_some_and(|s| s < size + 4096));
    let (start, end) = match v3 {
        Some(a) => (p_start - a, p_cur.wrapping_sub(a)),
        None => (field(buf, 12, 4)?, field(buf, 20, 4)?),
    };
    if !(0 < start && start <= end && end <= start + size) {
        return Err(bad(format!("implausible log bounds start={start} end={end} size={size}")));
    }
    let offset = |n: u64| usize::try_from(n).map_err(|_| bad(format!("offset {n} beyond memory")));
    Ok((offset(start)?, offset(end)?))
}

/// The dump kept in staging: the bytes from the header's address to the write position.
pub fn read_live(devmem: &Path, locator: &[u8]) -> io::Result<Vec<u8>> {
    let addr = field(locator, 4, 8)?;
    let mut f = File::open(devmem).map_err(at(devmem))?;
    let mut read = |n: usize| -> io::Result<Vec<u8>> {
        f.seek(SeekFrom::Start(addr))?;
        let mut buf = Vec::new();
        // only the bytes read take memory: a damaged header can ask for gigabytes
        (&f).take(n as u64).read_to_end(&mut buf)?;
        Ok(buf)
    };
    let (_, end) = bounds(&read(4096).map_err(at(devmem))?, Some(addr))?;
    let dump = read(end).map_err(at(devmem))?;
    if dump.len() != end {
        return Err(bad(format!("short read at {addr:#x}: {} of {end} bytes", dump.len())));
    }
    Ok(dump)
}

/// The identifier under which Project Mu firmware also hands out its memory log, as variables V0, V1, ... of
/// one block each (mu_basecore VariableSmm.c and mu_plus AdvLoggerMmAccessLib.c). The firmware lists none of them
/// and refuses every write to them.
pub const LOG_VARIABLE_GUID: &str = "a021bf2b-34ed-4a98-859c-420ef94f3e94";
const O_CREAT: i32 = 0o100; // Linux, every architecture this runs on

/// The same dump as `read_live`, read through the log variables: the way in when a kernel in lockdown mode
/// refuses /dev/mem. Opening a name with O_CREAT makes efivarfs ask the firmware for it; the kernel drops the
/// empty entry this leaves when the file is closed. NotFound if the firmware does not offer the log this way.
pub fn read_variables(efivars: &Path, locator: &[u8]) -> io::Result<Vec<u8>> {
    let addr = field(locator, 4, 8)?;
    let (mut dump, mut end) = (Vec::new(), None);
    for n in 0.. {
        if end.is_some_and(|e| dump.len() >= e) {
            break;
        }
        let path = efivars.join(format!("V{n}-{LOG_VARIABLE_GUID}"));
        // Rust's create() demands write access, which this must never ask for
        let mut f = OpenOptions::new().read(true).custom_flags(O_CREAT).mode(0o600).open(&path).map_err(at(&path))?;
        let mut block = Vec::new();
        f.read_to_end(&mut block).map_err(at(&path))?;
        if block.len() <= 4 {
            break; // past the last block the firmware answers "not found", which efivarfs shows as an empty file
        }
        dump.extend_from_slice(&block[4..]); // a variable's file starts with its 4 bytes of attributes
        if end.is_none() {
            end = Some(bounds(&dump, Some(addr))?.1);
        }
    }
    let Some(end) = end else {
        return Err(io::Error::new(io::ErrorKind::NotFound, format!("no log variable V0-{LOG_VARIABLE_GUID}")));
    };
    if dump.len() < end {
        return Err(bad(format!("log variables end at {} bytes, short of the write position {end}", dump.len())));
    }
    dump.truncate(end); // newer firmware hands out the whole buffer, unused bytes included
    Ok(dump)
}

/// The `len` bytes at `off`, or as many of them as there are.
fn clip(e: &[u8], off: usize, len: usize) -> &[u8] {
    &e[off.min(e.len())..(off + len).min(e.len())]
}

/// (log lines of a dump, bytes not decoded). A line takes the tick and level of the entry that ends it.
pub fn decode(dump: &[u8]) -> io::Result<(Vec<MemLine>, usize)> {
    // a dump ends at the write position, so the header's address is that position minus the dump's length
    let (start, end) = bounds(dump, field(dump, 16, 8)?.checked_sub(dump.len() as u64))?;
    let short = || bad(format!("{} bytes, short of the write position {end}", dump.len()));
    let entries = dump.get(..end).ok_or_else(short)?;
    let (freq, clock_tick) = (field(dump, 0x28, 8)?, field(dump, 0x30, 8)?);
    let c = dump.get(0x38..0x3f).ok_or_else(|| bad("no clock reading"))?;
    let clock = Time::new([le(&c[..2]) as i64, c[2].into(), c[3].into(), c[4].into(), c[5].into(), c[6].into()], 0);
    // tick * 1000 / frequency, rounded down, as the firmware does; i128 only for the product
    let line = |text: &[u8], tick: u64, level: u32| {
        let (ms, delta) = match i128::from(freq) {
            0 => (0, 0),
            f => (i128::from(tick) * 1000 / f, ((i128::from(tick) - i128::from(clock_tick)) * 1000).div_euclid(f)),
        };
        let time = clock.zip(i64::try_from(delta).ok()).and_then(|(c, d)| c.add_ms(d));
        MemLine { ms: i64::try_from(ms).unwrap_or(i64::MAX), time, level, text: norm(&latin1(text)) }
    };
    let mut lines = Vec::new();
    let mut pending = Vec::new();
    let mut last = (0, 0);
    let mut pos = start;
    let mut skipped = 0;
    while pos + 18 <= end {
        let e = &entries[pos..];
        let (tick, level, text, size) = if e.starts_with(b"ALMS") {
            let len = le(&e[16..18]) as usize;
            (le(&e[8..16]), le(&e[4..8]) as u32, clip(e, 18, len), (18 + len + 7) & !7)
        } else if e.starts_with(b"ALM2") && e.len() >= 24 {
            let (len, off) = (le(&e[20..22]) as usize, le(&e[22..24]) as usize);
            (le(&e[10..18]), le(&e[6..10]) as u32, clip(e, off, len), ((off + len + 7) & !7).max(8))
        } else {
            // not an entry the decoder knows: on to the next entry signature
            let Some(i) = e[1..].windows(4).position(|w| w == b"ALMS" || w == b"ALM2") else { break };
            skipped += i + 1;
            pos += i + 1;
            continue;
        };
        pending.extend_from_slice(text);
        last = (tick, level);
        while let Some(i) = pending.iter().position(|&b| b == b'\n') {
            lines.push(line(&pending[..i], tick, level));
            pending.drain(..=i);
        }
        pos += size;
    }
    if !pending.is_empty() {
        lines.push(line(&pending, last.0, last.1));
    }
    Ok((lines, skipped + end.saturating_sub(pos)))
}
