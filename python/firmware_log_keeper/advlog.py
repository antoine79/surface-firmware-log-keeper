"""Memory log: the Project Mu Advanced Logger buffer (SPEC 1.1).

Layout from mu_plus AdvLoggerPkg/Include/AdvancedLoggerInternal.h, decoders ported from the legacy keeper.
A line is (uptime in ms, firmware-clock time or None, level as "L%08x", message).
"""
import os
import re
import struct
from datetime import datetime, timedelta

from .merge import norm


def layout(buf, hdr_addr):
    """(start, end) of the entries, as offsets into buf, which begins at the header (ADVANCED_LOGGER_INFO)."""
    sig, _version = struct.unpack_from("<4sH", buf, 0)
    if sig != b"ALOG":
        raise ValueError(f"no ALOG signature at 0x{hdr_addr:x}")
    p_start, p_cur, _discarded, size = struct.unpack_from("<QQII", buf, 8)
    if hdr_addr <= p_start < hdr_addr + size + 4096:  # header version 3: physical addresses at 8 and 16
        start, end = p_start - hdr_addr, p_cur - hdr_addr
    else:  # newer Mu: u32 LogBufferOffset at 12, LogCurrentOffset at 20
        start, end = struct.unpack_from("<I", buf, 12)[0], struct.unpack_from("<I", buf, 20)[0]
    if not (0 < start <= end <= start + size):
        raise ValueError(f"implausible log bounds start={start} end={end} size={size}")
    return start, end


def read_live(devmem, locator):
    """The dump kept in staging: the bytes from the header's address to the write position."""
    hdr_addr = struct.unpack_from("<Q", locator, 4)[0]
    fd = os.open(devmem, os.O_RDONLY)
    try:
        _, end = layout(os.pread(fd, 4096, hdr_addr), hdr_addr)
        buf = os.pread(fd, end, hdr_addr)
    finally:
        os.close(fd)
    if len(buf) != end:
        raise ValueError(f"short read at 0x{hdr_addr:x}: {len(buf)} of {end} bytes")
    return buf


def whole(dump):
    """Can this be a complete dump? Used for a dump left under its temporary name: a cut one must not be merged.
    Its bounds must make sense and its first entry must sit where the header says."""
    try:
        start, end = layout(dump, struct.unpack_from("<Q", dump, 16)[0] - len(dump))
    except (ValueError, struct.error):
        return False
    return end == len(dump) and (start == end or dump[start:start + 4] in (b"ALMS", b"ALM2"))


def lines(dump):
    """(log lines of a dump, bytes not decoded). A line takes the tick and level of the entry that ends it.

    Bytes not decoded: those between the first entry and the write position that belong to no entry read (skipped
    to reach the next entry signature, or left when no further entry is found). Only the dump itself has them."""
    # A dump stops at the write position, so with the address layout the header's address is that position
    # minus the dump's length; with the offset layout the address is not needed.
    start, end = layout(dump, struct.unpack_from("<Q", dump, 16)[0] - len(dump))
    if end > len(dump):
        raise ValueError(f"dump of {len(dump)} bytes is shorter than its write position {end}")
    freq, clock_tick = struct.unpack_from("<QQ", dump, 0x28)
    try:
        clock = datetime(*struct.unpack_from("<H5B", dump, 0x38))
    except ValueError:
        clock = None

    def line(text, tick, level):
        ms = tick * 1000 // freq if freq else 0
        try:
            time = clock + timedelta(milliseconds=(tick - clock_tick) * 1000 // freq if freq else 0)
        except (TypeError, OverflowError):
            time = None
        return ms, time, f"L{level:08x}", norm(text)

    out, pending, last, off, skipped = [], "", None, start, 0
    while off + 18 <= end:
        sig = dump[off:off + 4]
        if sig == b"ALMS":
            level, tick, mlen = struct.unpack_from("<IQH", dump, off + 4)
            text, size = dump[off + 18:off + 18 + mlen], (18 + mlen + 7) & ~7
        elif sig == b"ALM2" and off + 24 <= end:
            level, tick, _phase, mlen, moff = struct.unpack_from("<IQHHH", dump, off + 6)
            text, size = dump[off + moff:off + moff + mlen], max((moff + mlen + 7) & ~7, 8)
        else:  # resync on the next entry signature
            nxt = min([p for p in (dump.find(b"ALMS", off + 1, end), dump.find(b"ALM2", off + 1, end)) if p >= 0],
                      default=-1)
            if nxt < 0:
                break
            skipped, off = skipped + nxt - off, nxt
            continue
        pending += text.decode("latin-1")
        last = (tick, level)
        while "\n" in pending:
            head, pending = pending.split("\n", 1)
            out.append(line(head, tick, level))
        off += size
    if pending:
        out.append(line(pending, *last))
    return out, skipped + max(end - off, 0)


TEXT_LINE = re.compile(r" *(\d+)\.(\d{3}) \S+ L([0-9a-f]{8})(?: (.*))?", re.S)
TEXT_CLOCK = re.compile(r"read at tick (\d+): (\d+)-(\d+)-(\d+) (\d+):(\d+):(\d+)")


def lines_from_text(text):
    """The log lines of a legacy advlog.txt, for a legacy folder whose advlog.bin is missing (SPEC 8)."""
    rows = text.split("\n")
    freq = re.search(r"timer (\d+) Hz", rows[0])
    clock = TEXT_CLOCK.search(rows[1]) if len(rows) > 1 else None
    zero = None  # firmware-clock time of uptime 0, rounded down as in lines()
    if freq and clock and int(freq[1]):
        tick, *ymdhms = map(int, clock.groups())
        try:
            zero = datetime(*ymdhms) + timedelta(milliseconds=-tick * 1000 // int(freq[1]))
        except (ValueError, OverflowError):
            pass
    out = []
    for row in rows:
        if not row or row.startswith("#"):
            continue
        m = TEXT_LINE.fullmatch(row)
        if not m:
            raise ValueError(f"legacy log line not understood: {row[:60]!r}")
        ms = int(m[1]) * 1000 + int(m[2])
        out.append((ms, zero + timedelta(milliseconds=ms) if zero else None, "L" + m[3], norm(m[4] or "")))
    return out
