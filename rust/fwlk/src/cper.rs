//! Error record: 4 attribute bytes + CPER record (SPEC 1.3). Format decoding only.
//!
//! Layouts: UEFI specification appendix N (CPER), mu_plus MsWheaPkg/Include/Guid/MuTelemetryCperSection.h.
use crate::le;
use crate::time::Time;

/// An error record as its row shows it.
#[derive(Clone, Debug, PartialEq)]
pub struct Record {
    pub hash: String,
    pub time: Option<Time>,
    pub id: u64,
    pub text: String,
}

const MU_TELEMETRY: &str = "85183a8b-9c41-429c-939c-5c3c087ca280";

fn bcd(b: u8) -> i64 {
    i64::from(b >> 4) * 10 + i64::from(b & 0xf)
}

/// A GUID stored with its first three fields little-endian, in the usual lower-case form.
fn guid(b: &[u8]) -> String {
    let hex = |r: &[u8]| r.iter().map(|x| format!("{x:02x}")).collect::<String>();
    format!("{:08x}-{:04x}-{:04x}-{}-{}", le(&b[0..4]), le(&b[4..6]), le(&b[6..8]), hex(&b[8..10]), hex(&b[10..16]))
}

/// The record of a whole variable content, whose sha16 is `hash`.
pub fn decode(raw: &[u8], hash: &str) -> Record {
    decoded(raw, hash).unwrap_or_else(|| Record {
        hash: hash.into(), time: None, id: 0, text: format!("HwErrRec undecoded hash={hash} length={}", raw.len()),
    })
}

fn decoded(raw: &[u8], hash: &str) -> Option<Record> {
    let d = raw.get(4..).filter(|d| d.starts_with(b"CPER") && d.len() >= 128)?;
    let (sections, severity, valid, length) = (le(&d[10..12]) as usize, le(&d[12..16]), le(&d[16..20]), le(&d[20..24]));
    if d.len() < 128 + 72 * sections || (d.len() as u64) < length {
        return None;
    }
    // the two-digit year is used as 20YY; the raw century byte is kept in the text
    let [s, mi, h, _, day, mo, y, century] = d[24..32] else { return None };
    let stamped = valid & 2 != 0;
    let time = stamped.then(|| Time::new([2000 + bcd(y), bcd(mo), bcd(day), bcd(h), bcd(mi), bcd(s)], 0)).flatten();
    let id = le(&d[96..104]);
    let severity = match severity {
        0 => "recoverable".to_string(),
        1 => "fatal".to_string(),
        2 => "corrected".to_string(),
        3 => "informational".to_string(),
        n => n.to_string(),
    };
    let mut text = format!("HwErrRec record_id=0x{id:x} severity={severity} hash={hash} century={century:02x}");
    for o in (128..).step_by(72).take(sections) {
        let (off, len) = (le(&d[o..o + 4]) as usize, le(&d[o + 4..o + 8]) as usize);
        let (kind, body) = (guid(&d[o + 16..o + 32]), d.get(off..off.checked_add(len)?)?);
        text += &format!(" | section type={kind}");
        if kind == MU_TELEMETRY && body.len() >= 56 {
            let (component, sub) = (guid(&body[0..16]), guid(&body[16..32]));
            text += &format!(" component={component} subcomponent={sub} status=0x{:08x} info1=0x{:x} info2=0x{:x}",
                             le(&body[36..40]), le(&body[40..48]), le(&body[48..56]));
        }
    }
    Some(Record { hash: hash.into(), time, id, text })
}
