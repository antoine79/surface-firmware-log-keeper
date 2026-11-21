//! Error record: 4 attribute bytes + CPER record (SPEC 1.3). Format decoding only.
//!
//! Layouts: UEFI specification appendix N (CPER), mu_plus MsWheaPkg/Include/Guid/MuTelemetryCperSection.h.
use crate::le;
use crate::time::Time;

/// (timestamp, record id, message of the row) of an error record.
pub type Record = (Option<Time>, u64, String);

const MU_TELEMETRY: &str = "85183a8b-9c41-429c-939c-5c3c087ca280";

fn bcd(b: u8) -> i64 {
    (b >> 4) as i64 * 10 + (b & 0xf) as i64
}

/// A GUID stored with its first three fields little-endian, in the usual lower-case form.
fn guid(b: &[u8]) -> String {
    let h = |r: &[u8]| r.iter().map(|x| format!("{x:02x}")).collect::<String>();
    let rev = |r: &[u8]| h(&r.iter().rev().copied().collect::<Vec<_>>());
    format!("{}-{}-{}-{}-{}", rev(&b[0..4]), rev(&b[4..6]), rev(&b[6..8]), h(&b[8..10]), h(&b[10..16]))
}

/// (timestamp or None, record id, message of the row) of a whole variable content.
pub fn decode(raw: &[u8], sha: &str) -> Record {
    let d = raw.get(4..).unwrap_or(&[]);
    let undecoded = (None, 0, format!("HwErrRec undecoded hash={sha} length={}", raw.len()));
    if !d.starts_with(b"CPER") || d.len() < 128 {
        return undecoded;
    }
    let (nsec, sev, valid, rlen) = (le(&d[10..12]) as usize, le(&d[12..16]), le(&d[16..20]), le(&d[20..24]));
    if d.len() < 128 + 72 * nsec || (d.len() as u64) < rlen {
        return undecoded;
    }
    let mut time = None;
    if valid & 2 != 0 {
        let [s, mi, h, _flag, day, mo, y, _century] = [d[24], d[25], d[26], d[27], d[28], d[29], d[30], d[31]];
        // the two-digit year is used as 20YY; the raw century byte is kept in the text
        time = Time::new(2000 + bcd(y), bcd(mo), bcd(day), bcd(h), bcd(mi), bcd(s), 0);
    }
    let rec_id = le(&d[96..104]);
    let severity = match sev {
        0 => "recoverable".to_string(),
        1 => "fatal".to_string(),
        2 => "corrected".to_string(),
        3 => "informational".to_string(),
        n => n.to_string(),
    };
    let mut text = format!("HwErrRec record_id=0x{rec_id:x} severity={severity} hash={sha} century={:02x}", d[31]);
    for i in 0..nsec {
        let o = 128 + 72 * i;
        let (off, ln) = (le(&d[o..o + 4]) as usize, le(&d[o + 4..o + 8]) as usize);
        if off + ln > d.len() {
            return undecoded;
        }
        let (kind, body) = (guid(&d[o + 16..o + 32]), &d[off..off + ln]);
        text += &format!(" | section type={kind}");
        if kind == MU_TELEMETRY && body.len() >= 56 {
            text += &format!(" component={} subcomponent={} status=0x{:08x} info1=0x{:x} info2=0x{:x}",
                             guid(&body[0..16]), guid(&body[16..32]), le(&body[36..40]), le(&body[40..48]),
                             le(&body[48..56]));
        }
    }
    (time, rec_id, text)
}
