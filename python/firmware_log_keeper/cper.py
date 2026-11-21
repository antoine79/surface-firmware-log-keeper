"""Error record: 4 attribute bytes + CPER record (SPEC 1.3). Format decoding only.

Layouts: UEFI specification appendix N (CPER), mu_plus MsWheaPkg/Include/Guid/MuTelemetryCperSection.h.
"""
import struct
import uuid
from datetime import datetime

SEVERITY = {0: "recoverable", 1: "fatal", 2: "corrected", 3: "informational"}
MU_TELEMETRY = uuid.UUID("85183a8b-9c41-429c-939c-5c3c087ca280")


def bcd(b):
    return (b >> 4) * 10 + (b & 0xF)


def decode(raw, sha):
    """(timestamp or None, record id, message of the row) of a whole variable content."""
    d = raw[4:]
    undecoded = None, 0, f"HwErrRec undecoded hash={sha} length={len(raw)}"
    if d[:4] != b"CPER" or len(d) < 128:
        return undecoded
    nsec, sev, valid, rlen = struct.unpack_from("<HIII", d, 10)
    if len(d) < 128 + 72 * nsec or len(d) < rlen:
        return undecoded
    time = None
    if valid & 2:
        s, mi, h, _flag, day, mo, y, _century = d[24:32]
        try:  # the two-digit year is used as 20YY; the raw century byte is kept in the text
            time = datetime(2000 + bcd(y), bcd(mo), bcd(day), bcd(h), bcd(mi), bcd(s))
        except ValueError:
            pass
    rec_id = struct.unpack_from("<Q", d, 96)[0]
    text = f"HwErrRec record_id=0x{rec_id:x} severity={SEVERITY.get(sev, sev)} hash={sha} century={d[31]:02x}"
    for i in range(nsec):
        o = 128 + 72 * i
        off, ln = struct.unpack_from("<II", d, o)
        if off + ln > len(d):
            return undecoded
        kind, body = uuid.UUID(bytes_le=d[o + 16:o + 32]), d[off:off + ln]
        text += f" | section type={kind}"
        if kind == MU_TELEMETRY and len(body) >= 56:
            status, info1, info2 = struct.unpack_from("<IQQ", body, 36)
            text += (f" component={uuid.UUID(bytes_le=body[0:16])} subcomponent={uuid.UUID(bytes_le=body[16:32])}"
                     f" status=0x{status:08x} info1=0x{info1:x} info2=0x{info2:x}")
    return time, rec_id, text
