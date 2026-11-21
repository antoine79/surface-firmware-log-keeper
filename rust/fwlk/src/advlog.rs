//! Memory log: the Project Mu Advanced Logger buffer (SPEC 1.1).
//!
//! Layout from mu_plus AdvLoggerPkg/Include/AdvancedLoggerInternal.h, decoders ported from the Python reference.
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::path::Path;

use crate::filelog::latin1;
use crate::merge::norm;
use crate::time::Time;
use crate::{le, Error, Res};

/// The `size` bytes at `off`, or an error for a buffer too short.
fn unpack(buf: &[u8], off: usize, size: usize) -> Res<&[u8]> {
    buf.get(off..off + size).ok_or_else(|| Error::value(format!("{} bytes, too short for {size} bytes at {off}", buf.len())))
}

/// One log line: uptime in ms, firmware-clock time, level as "L%08x", message.
#[derive(Clone, Debug, PartialEq)]
pub struct Line {
    pub ms: i128,
    pub time: Option<Time>,
    pub level: String,
    pub message: String,
}

/// (start, end) of the entries, as offsets into buf, which begins at the header (ADVANCED_LOGGER_INFO).
pub fn layout(buf: &[u8], hdr_addr: i128) -> Res<(i128, i128)> {
    unpack(buf, 0, 6)?;
    if &buf[..4] != b"ALOG" {
        return Err(Error::value(format!("no ALOG signature at {hdr_addr:#x}")));
    }
    let h = unpack(buf, 8, 24)?;
    let (p_start, p_cur, size) = (le(&h[0..8]) as i128, le(&h[8..16]) as i128, le(&h[20..24]) as i128);
    let (start, end) = if hdr_addr <= p_start && p_start < hdr_addr + size + 4096 {
        (p_start - hdr_addr, p_cur - hdr_addr) // header version 3: physical addresses at 8 and 16
    } else {
        (le(&buf[12..16]) as i128, le(&buf[20..24]) as i128) // newer Mu: u32 offsets at 12 and 20
    };
    if !(0 < start && start <= end && end <= start + size) {
        return Err(Error::value(format!("implausible log bounds start={start} end={end} size={size}")));
    }
    Ok((start, end))
}

/// The dump kept in staging: the bytes from the header's address to the write position.
pub fn read_live(devmem: &Path, locator: &[u8]) -> Res<Vec<u8>> {
    let hdr_addr = le(unpack(locator, 4, 8)?);
    let f = File::open(devmem).map_err(|e| Error::io(&e, devmem))?;
    let pread = |n: usize| -> Res<Vec<u8>> {
        if hdr_addr > i64::MAX as u64 {
            return Err(Error::value(format!("address {hdr_addr:#x} beyond the file offsets")));
        }
        let mut buf = Vec::new();
        buf.try_reserve_exact(n).map_err(|_| Error::value(format!("no memory for {n} bytes")))?;
        // os.pread: one read, short at the end of the file or at a part of memory that cannot be read; done in
        // steps here, so that only the bytes read take memory (a damaged header can ask for gigabytes)
        let mut step = vec![0; n.min(1 << 20)];
        while buf.len() < n {
            let want = (n - buf.len()).min(step.len());
            match f.read_at(&mut step[..want], hdr_addr + buf.len() as u64) {
                Ok(got) => {
                    buf.extend_from_slice(&step[..got]);
                    if got < want {
                        break;
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) if buf.is_empty() => return Err(Error::io(&e, devmem)),
                Err(_) => break,
            }
        }
        Ok(buf)
    };
    let (_, end) = layout(&pread(4096)?, hdr_addr as i128)?;
    let n = usize::try_from(end).map_err(|_| Error::value(format!("write position {end} beyond memory")))?;
    let buf = pread(n)?;
    if buf.len() != n {
        return Err(Error::value(format!("short read at 0x{hdr_addr:x}: {} of {end} bytes", buf.len())));
    }
    Ok(buf)
}

/// The header's address of a dump: the write position minus the dump's length with the address layout.
fn dump_addr(dump: &[u8]) -> Res<i128> {
    Ok(le(unpack(dump, 16, 8)?) as i128 - dump.len() as i128)
}

/// Can this be a complete dump? Used for a dump left under its temporary name: a cut one must not be merged.
pub fn whole(dump: &[u8]) -> bool {
    let Ok((start, end)) = dump_addr(dump).and_then(|a| layout(dump, a)) else { return false };
    end == dump.len() as i128 && (start == end || {
        let s = start as usize;
        matches!(dump.get(s..s + 4), Some(b"ALMS" | b"ALM2"))
    })
}

/// (log lines of a dump, bytes not decoded). A line takes the tick and level of the entry that ends it.
pub fn lines(dump: &[u8]) -> Res<(Vec<Line>, i128)> {
    let (start, end) = layout(dump, dump_addr(dump)?)?;
    if end > dump.len() as i128 {
        return Err(Error::value(format!("dump of {} bytes is shorter than its write position {end}", dump.len())));
    }
    let (start, end) = (start as usize, end as usize);
    let t = unpack(dump, 0x28, 16)?;
    let (freq, clock_tick) = (le(&t[..8]) as i128, le(&t[8..]) as i128);
    let c = unpack(dump, 0x38, 7)?;
    let clock = Time::new(le(&c[..2]) as i64, c[2] as i64, c[3] as i64, c[4] as i64, c[5] as i64, c[6] as i64, 0);
    let line = |text: &[u8], tick: u64, level: u32| {
        let tick = tick as i128;
        let ms = if freq != 0 { tick * 1000 / freq } else { 0 };
        let delta = if freq != 0 { ((tick - clock_tick) * 1000).div_euclid(freq) } else { 0 };
        Line { ms, time: clock.and_then(|c| c.add_ms(delta)), level: format!("L{level:08x}"),
               message: norm(&latin1(text)) }
    };
    let text_at = |a: usize, n: usize| &dump[a.min(dump.len())..(a + n).min(dump.len())];
    let (mut out, mut pending, mut last, mut off, mut skipped) = (Vec::new(), Vec::new(), (0, 0), start, 0usize);
    while off + 18 <= end {
        let sig = &dump[off..off + 4];
        let (level, tick, text, size);
        if sig == b"ALMS" {
            let mlen = le(&dump[off + 16..off + 18]) as usize;
            (level, tick) = (le(&dump[off + 4..off + 8]) as u32, le(&dump[off + 8..off + 16]));
            (text, size) = (text_at(off + 18, mlen), (18 + mlen + 7) & !7);
        } else if sig == b"ALM2" && off + 24 <= end {
            let (mlen, moff) = (le(&dump[off + 20..off + 22]) as usize, le(&dump[off + 22..off + 24]) as usize);
            (level, tick) = (le(&dump[off + 6..off + 10]) as u32, le(&dump[off + 10..off + 18]));
            (text, size) = (text_at(off + moff, mlen), ((moff + mlen + 7) & !7).max(8));
        } else {
            // resync on the next entry signature
            let find = |s: &[u8]| dump[off + 1..end].windows(4).position(|w| w == s);
            let Some(nxt) = [find(b"ALMS"), find(b"ALM2")].into_iter().flatten().min() else { break };
            skipped += nxt + 1;
            off += nxt + 1;
            continue;
        }
        pending.extend_from_slice(text);
        last = (tick, level);
        while let Some(i) = pending.iter().position(|&b| b == b'\n') {
            out.push(line(&pending[..i], tick, level));
            pending.drain(..=i);
        }
        off += size;
    }
    if !pending.is_empty() {
        out.push(line(&pending, last.0, last.1));
    }
    Ok((out, skipped as i128 + (end as i128 - off as i128).max(0)))
}
