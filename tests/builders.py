"""Builders for the firmware-log-keeper test suite.

Everything the firmware leaves behind is synthesised here from scratch: the Advanced Logger memory buffer, the
UEFI_Log files, the error-record variables, the timings and versions trees. The file
also holds the expected result of a merge (SPEC.md section 3), written independently of any keeper, and the readers
for the archive files.

Nothing here imports the keeper: the tests drive it as a black box (the executable named by FWLK_BIN).
"""
from __future__ import annotations

import copy
import hashlib
import re
import struct
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

HWERR_GUID = "414e6bdd-e47b-47cc-b244-bb61020cf516"
LOCATOR_VAR = "AdvLoggerLocator-4d60cfb5-f481-4a98-9c81-bff86460c43e"
SUPPORT_VAR = "HwErrRecSupport-8be4df61-93ca-11d2-aa0d-00e098032b8c"
MU_TELEMETRY = uuid.UUID("85183a8b-9c41-429c-939c-5c3c087ca280")
COMPONENT = uuid.UUID("11111111-2222-3333-4444-555555555555")
NIL = uuid.UUID(int=0)
SEVERITY = {0: "recoverable", 1: "fatal", 2: "corrected", 3: "informational"}

COLUMNS = "time\tuptime_s\tseg\tlevel\tsource\tmem_line\tfile_line\tmessage\n"
SLOT_SIZE = 4 * 1024 * 1024
PAD_BYTES = b" \x00\r\n"  # what the firmware pads a file log with; the content is the file without this tail
FREQ = 1_000_000  # timer ticks per second of the synthetic memory logs: 1000 ticks per millisecond
HEADER_ADDR = 0x1_2345_6000  # above 4 GiB, so an implementation that keeps the address in 32 bits fails
HEADER_SIZE = 0x50
CAPACITY = 0x10000
MS = timedelta(milliseconds=1)


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def norm(text: str) -> str:
    """The message column: trailing spaces, tabs, CR and LF removed, tabs replaced by spaces."""
    return text.rstrip(" \t\r\n").replace("\t", " ")


def fmt_time(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def fmt_uptime(ms: int) -> str:
    """Seconds with 3 decimals. An error record of a run's first second can be before the reset: -500 ms is -0.500."""
    return f"{'-' if ms < 0 else ''}{abs(ms) // 1000}.{abs(ms) % 1000:03d}"


def uptime_ms(text: str) -> int:
    whole, frac = text.lstrip("-").split(".")
    return (int(whole) * 1000 + int(frac)) * (-1 if text.startswith("-") else 1)


def label(t: datetime) -> str:
    """The time part of a run folder name."""
    return t.strftime("%Y-%m-%dT%H%M%S")


def utc_stamp(t: datetime) -> str:
    """The UTC time of a keeper run, as written in the seen.tsv files and in the staging folder name."""
    return t.strftime("%Y-%m-%dT%H%M%SZ")


# ---------------------------------------------------------------------------------------------------------------
# Log lines


@dataclass(frozen=True)
class L:
    """One line the firmware logs: uptime in milliseconds, text, debug level (the level exists in memory only)."""
    ms: int
    text: str
    level: int = 0x80000000


def boot_lines(tag: str, n: int = 8) -> list[L]:
    """The lines of one firmware run. Every text carries the tag, so two runs with different tags share no line.

    The last line is at 5.000 s: a file log flushed there has a whole-second END_OF_LOG clock.
    """
    lines = [L(374 + 137 * i, f"{tag} step {i}", 0x80000000 + 4 * (i % 3)) for i in range(n)]
    return lines + [L(5000, f"{tag} boot loader", 0x80000040)]


# ---------------------------------------------------------------------------------------------------------------
# Memory log (Advanced Logger buffer), in the layout of mu_plus AdvLoggerPkg/Include/AdvancedLoggerInternal.h


@dataclass(frozen=True)
class Entry:
    tick: int
    text: bytes
    level: int = 0x80000000
    kind: str = "ALMS"  # or "ALM2", the newer entry layout


@dataclass(frozen=True)
class MemLine:
    n: int  # line number in the memory log, 1-based
    ms: int
    time: datetime
    level: int
    text: str


class MemLog:
    """A memory log: header (ALOG) then message entries, each padded to 8 bytes.

    layout "addresses" is header version 3 (physical addresses of the buffer start and of the write position at
    bytes 8 and 16); "offsets" is the newer header (32-bit offsets at bytes 12 and 20).
    """

    def __init__(self, entries, clock: datetime, clock_tick: int = 2 * FREQ, freq: int = FREQ,
                 addr: int = HEADER_ADDR, layout: str = "addresses"):
        self.entries, self.clock, self.clock_tick, self.freq = list(entries), clock, clock_tick, freq
        self.addr, self.layout = addr, layout

    @classmethod
    def from_lines(cls, lines, reset: datetime, **kw) -> "MemLog":
        """One entry per line, ending in CRLF as the firmware writes them; the clock is read 2 s after reset."""
        entries = [Entry(line.ms * FREQ // 1000, (line.text + "\r\n").encode("latin-1"), line.level) for line in lines]
        return cls(entries, clock=reset + timedelta(seconds=2), **kw)

    @staticmethod
    def _entry(e: Entry) -> bytes:
        if e.kind == "ALMS":  # signature, level, tick, text length, text
            raw = struct.pack("<4sIQH", b"ALMS", e.level, e.tick, len(e.text)) + e.text
        else:  # signature, version 2.0, level, tick, phase, text length, text offset, text
            raw = struct.pack("<4sBBIQHHH", b"ALM2", 2, 0, e.level, e.tick, 4, len(e.text), 24) + e.text
        return raw + bytes(-len(raw) % 8)

    def buffer(self) -> bytes:
        """Header and used entries: what the keeper reads from /dev/mem and stages as advlog.bin."""
        body = b"".join(self._entry(e) for e in self.entries)
        end = HEADER_SIZE + len(body)
        head = bytearray(HEADER_SIZE)
        if self.layout == "addresses":
            struct.pack_into("<4sH", head, 0, b"ALOG", 3)
            struct.pack_into("<QQ", head, 8, self.addr + HEADER_SIZE, self.addr + end)
        else:
            struct.pack_into("<4sH", head, 0, b"ALOG", 5)
            struct.pack_into("<I", head, 12, HEADER_SIZE)
            struct.pack_into("<I", head, 20, end)
        struct.pack_into("<II", head, 24, 0, max(CAPACITY, len(body)))  # bytes discarded, buffer size
        struct.pack_into("<QQ", head, 0x28, self.freq, self.clock_tick)
        c = self.clock
        struct.pack_into("<H5B", head, 0x38, c.year, c.month, c.day, c.hour, c.minute, c.second)
        return bytes(head) + body

    @property
    def hash(self) -> str:
        return sha16(self.buffer())

    def time_of(self, tick: int) -> datetime:
        """Firmware-clock time of a tick: the clock reading plus the tick difference, rounded down to the ms."""
        return self.clock + ((tick - self.clock_tick) * 1000 // self.freq) * MS

    @property
    def reset(self) -> datetime:
        return self.time_of(0)

    def lines(self) -> list[MemLine]:
        """The log lines. A line built from several entries has the tick and level of the entry that ends it."""
        out, pending, last = [], "", None
        for e in self.entries:
            pending += e.text.decode("latin-1")
            last = e
            while "\n" in pending:
                head, pending = pending.split("\n", 1)
                out.append(MemLine(len(out) + 1, e.tick * 1000 // self.freq, self.time_of(e.tick), e.level, head))
        if pending:
            out.append(MemLine(len(out) + 1, last.tick * 1000 // self.freq, self.time_of(last.tick), last.level, pending))
        return out

    def write(self, devmem: Path, efivars: Path) -> None:
        """The fake /dev/mem (a sparse file with the buffer at its address) and the locator variable."""
        with open(devmem, "wb") as f:
            f.seek(self.addr)
            f.write(self.buffer() + b"\xee" * 64)  # bytes past the write position must not be read
        (efivars / LOCATOR_VAR).write_bytes(struct.pack("<IQ", 6, self.addr))


# ---------------------------------------------------------------------------------------------------------------
# File log (UEFI_Log<n>.txt)

LINE_RE = re.compile(rb"(\d\d):(\d\d):(\d\d)\.(\d{3}) :(?: (.*))?", re.S)
END_MARK = b"=== END_OF_LOG ==="


# The tags newer Project Mu firmware writes before each line of the file log (mu_plus AdvLoggerPkg,
# AdvancedLoggerAccessLib.c): the boot phase of the entry, then the name of the first known bit of its debug level.
PHASE_DXE = "[DXE  ] "
LEVEL_TAGS = [("[INIT]", 0x1), ("[WARN]", 0x2), ("[LOAD]", 0x4), ("[FS  ]", 0x8), ("[POOL]", 0x10), ("[PAGE]", 0x20),
              ("[INFO]", 0x40), ("[DISP]", 0x80), ("[VARI]", 0x100), ("[SMI ]", 0x200), ("[BM  ]", 0x400),
              ("[BLIO]", 0x1000), ("[NETw]", 0x4000), ("[UNDI]", 0x10000), ("[LDFL]", 0x20000), ("[EVNT]", 0x80000),
              ("[GCD ]", 0x100000), ("[CACH]", 0x200000), ("[VERB]", 0x400000), ("[MBTY]", 0x800000),
              ("[ERR ]", 0x80000000)]


def level_tag(level: int) -> str:
    """The level tag and its space; nothing for a level with no known bit."""
    return next((name + " " for name, bit in LEVEL_TAGS if level & bit), "")


def _after_tag(text: str, inside) -> str:
    """The text after a leading "[...]" whose inside passes the test, and after the space that follows it."""
    close = text.find("]")
    if text.startswith("[") and close > 0 and inside(text[1:close]) and text[close + 1:close + 2] in ("", " "):
        return text[close + 2:]
    return text


def untag(text: str) -> str:
    """A file-log line without the tags (SPEC 3.3): a phase tag (5 characters, or 4 hex digits, in brackets), then
    a level tag (4 characters in brackets), each followed by a space or ending the line."""
    text = _after_tag(text, lambda t: len(t) == 5 or len(t) == 4 and all(c in "0123456789abcdefABCDEF" for c in t))
    return _after_tag(text, lambda t: len(t) == 4)


def stamp(ms: int) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    return f"{h:02d}:{m:02d}:{rest // 1000:02d}.{rest % 1000:03d}"


@dataclass(frozen=True)
class FileLine:
    n: int  # line number in the file, 1-based, as an editor shows it
    ms: int
    text: str


def parse_content(content: bytes) -> list[FileLine]:
    """The log lines of a file log content, read as SPEC 1.2 says. Used to check the merged log against the raw copy.

    A line ends at LF; one CR just before it is not part of the line. Only the lines before the first END_OF_LOG
    count. A blank line is not a log line but keeps its number. A line that does not parse keeps the uptime of the
    line before it and its raw text.
    """
    out, prev = [], 0
    for n, raw in enumerate(content.split(b"\n"), 1):
        raw = raw[:-1] if raw.endswith(b"\r") else raw
        if END_MARK in raw:
            break
        if not raw:
            continue
        m = LINE_RE.fullmatch(raw)
        if m:
            prev = ((int(m[1]) * 60 + int(m[2])) * 60 + int(m[3])) * 1000 + int(m[4])
        out.append(FileLine(n, prev, (m[5] or b"" if m else raw).decode("latin-1")))
    return out


def padding(n: int, style: str) -> bytes:
    if style == "lines":  # what the Surface firmware does: lines of 70 spaces
        return ((b" " * 70 + b"\r\n") * (n // 72 + 1))[:n]
    if style == "spaces":
        return b" " * n
    if style == "nul":
        return bytes(n)
    if style == "mixed":
        return b" " * (n // 2) + bytes(n - n // 2)
    raise ValueError(style)


class FileLog:
    """One UEFI_Log file as the firmware writes it.

    lines: L objects (written as "HH:MM:SS.mmm : text") or bytes (a raw line, for lines the keeper cannot parse).
    end_clock: the firmware clock in the END_OF_LOG line; None for a log that was never flushed (no END_OF_LOG).
    tail: bytes of an older, longer log left in the slot after this one.
    phase: None for the firmware of the Surface Book 2, which writes no tag; else what newer firmware writes before
    each text: this phase tag (PHASE_DXE, or "" for an entry of the older type, which has no phase) and the level tag.
    """

    def __init__(self, lines, end_clock: datetime | None, *, tail: bytes = b"", pad: str = "lines", size: int = SLOT_SIZE,
                 phase: str | None = None):
        self.end_clock, self.tail, self.pad, self.size = end_clock, tail, pad, size

        def tags(x):
            return "" if phase is None else phase + level_tag(x.level)

        raw = [x if isinstance(x, bytes) else f"{stamp(x.ms)} : {tags(x)}{x.text}".encode("latin-1") for x in lines]
        self.body = b"".join(r + b"\r\n" for r in raw)
        self.end_offset = None  # offset of the end of the END_OF_LOG line (line break not included)
        if end_clock is not None:
            self.body += b"\r\n\r\n ===" + b" END_OF_LOG === @ === " + self.end_as_written.encode() + b" ==="
            self.end_offset = len(self.body)
            self.body += b"\r\n\r\n"

    @property
    def end_as_written(self) -> str | None:
        c = self.end_clock
        return c and f"{c.year}/{c.month}/{c.day} {c.hour}:{c.minute:02d}:{c.second:02d}"

    def over(self, older: "FileLog") -> "FileLog":
        """This log written in a slot that held an older, longer one: the end of the older log stays behind it."""
        assert len(older.body) > len(self.body)
        new = copy.copy(self)
        new.tail = older.body[len(self.body):]
        return new

    def file_bytes(self) -> bytes:
        used = self.body + self.tail
        return used + padding(self.size - len(used), self.pad)

    @property
    def content(self) -> bytes:
        """What the keeper archives: the bytes before the padding, unmodified."""
        return self.file_bytes().rstrip(PAD_BYTES)

    @property
    def hash(self) -> str:
        return sha16(self.content)

    @property
    def complete(self) -> bool:
        return self.end_clock is not None

    @property
    def trailing(self) -> int:
        """Content bytes after the first END_OF_LOG line."""
        return len(self.content) - self.end_offset if self.complete else 0

    def rows(self) -> list[FileLine]:
        return parse_content(self.content)


# ---------------------------------------------------------------------------------------------------------------
# Error records (HwErrRec<nnnn> variables): 4 attribute bytes + a CPER record (UEFI specification, appendix N)


@dataclass(frozen=True)
class Record:
    raw: bytes  # the whole variable content
    ts: datetime | None  # None for a record the keeper cannot decode
    record_id: int
    message: str  # the message column of its row

    @property
    def hash(self) -> str:
        return sha16(self.raw)


def _bcd(n: int) -> int:
    return (n // 10) << 4 | n % 10


def record(ts: datetime, record_id: int = 0x1A, *, severity: int = 1, century: int = 0x20, status: int = 0x02010007,
           info1: int = 0, info2: int = 0, component: uuid.UUID = COMPONENT, subcomponent: uuid.UUID = NIL,
           other_sections=(), attrs: int = 0x0F) -> Record:
    """A CPER record with one Project Mu telemetry section, then one section per (type, body) of other_sections."""
    mu = component.bytes_le + subcomponent.bytes_le + bytes(4) + struct.pack("<IQQ", status, info1, info2)
    sections = [(MU_TELEMETRY, mu), *other_sections]
    table, bodies = b"", b""
    for kind, body in sections:
        d = bytearray(72)  # section descriptor: offset, length, revision, ..., section type at 16
        struct.pack_into("<IIH", d, 0, 128 + 72 * len(sections) + len(bodies), len(body), 0x0100)
        d[16:32] = kind.bytes_le
        table, bodies = table + d, bodies + body
    head = bytearray(128)
    struct.pack_into("<4sHI", head, 0, b"CPER", 0x0101, 0xFFFFFFFF)
    struct.pack_into("<HIII", head, 10, len(sections), severity, 3, 128 + len(table) + len(bodies))  # 3: time valid
    head[24:32] = bytes([_bcd(ts.second), _bcd(ts.minute), _bcd(ts.hour), 0,
                         _bcd(ts.day), _bcd(ts.month), _bcd(ts.year % 100), century])
    struct.pack_into("<Q", head, 96, record_id)
    raw = struct.pack("<I", attrs) + bytes(head) + table + bodies
    message = (f"HwErrRec record_id=0x{record_id:x} severity={SEVERITY.get(severity, severity)} hash={sha16(raw)} "
               f"century={century:02x} | section type={MU_TELEMETRY} component={component} "
               f"subcomponent={subcomponent} status=0x{status:08x} info1=0x{info1:x} info2=0x{info2:x}"
               + "".join(f" | section type={kind}" for kind, _ in other_sections))
    return Record(raw, ts, record_id, message)


def undecodable(raw: bytes) -> Record:
    """A variable whose content is not a readable CPER record: archived raw, one row without time."""
    return Record(raw, None, 0, f"HwErrRec undecoded hash={sha16(raw)} length={len(raw)}")


def record_var(slot: int, guid: str = HWERR_GUID) -> str:
    return f"HwErrRec{slot:04X}-{guid}"


# ---------------------------------------------------------------------------------------------------------------
# The expected merged log (SPEC section 3)


@dataclass(frozen=True)
class Row:
    time: str
    uptime: str
    seg: int
    level: str
    source: str
    mem_line: str
    file_line: str
    message: str

    def text(self) -> str:
        return "\t".join([self.time, self.uptime, str(self.seg), self.level, self.source, self.mem_line,
                          self.file_line, self.message])


def record_id_of(message: str) -> int:
    m = re.search(r"record_id=0x([0-9a-f]+)", message)
    return int(m[1], 16) if m else 0


def sort_key(row: Row):
    """Order of a run's firmware.log (SPEC 3.4): segment, uptime, source, then line number, or record id and hash."""
    number = next((int(x) for x in (row.mem_line, row.file_line) if x != "-"), None)
    if number is None:
        return row.seg, uptime_ms(row.uptime), row.source, record_id_of(row.message), unattached_key(row)[2]
    return row.seg, uptime_ms(row.uptime), row.source, number, ""


def unattached_key(row: Row):
    """Order of unattached/firmware.log: time, record id, hash."""
    return row.time, record_id_of(row.message), re.search(r"hash=([0-9a-f]{16})", row.message)[1]


def _keys(lines) -> list[tuple]:
    """Row keys (SPEC 3.3) of the lines of one source: (segment, uptime in ms, message, occurrence)."""
    seg, prev, count, out = 0, None, {}, []
    for line in lines:
        if prev is not None and line.ms < prev:
            seg += 1
        prev = line.ms
        key = (seg, line.ms, norm(line.text))
        count[key] = count.get(key, 0) + 1
        out.append(key + (count[key],))
    return out


def merge(mem: MemLog | None = None, file: FileLog | None = None, records=()) -> list[Row]:
    """The rows of one run's firmware.log, in order, from its memory log, its file log and its error records.

    A file given with a memory log must be of that run (SPEC 3.1): every one of its row keys among the memory log's,
    as it is or without its tags. A file-only run is merge(None, file)."""
    mem_lines = mem.lines() if mem else []
    file_lines = file.rows() if file else []
    mem_keys = _keys(mem_lines)
    file_keys = _keys(file_lines)
    if mem and not set(mem_keys).issuperset(file_keys):  # as it is first, else without the tags
        file_keys = _keys([FileLine(x.n, x.ms, untag(norm(x.text))) for x in file_lines])
        assert set(mem_keys).issuperset(file_keys), "this file log is another run (SPEC 3.1)"
    in_file = dict(zip(file_keys, file_lines))
    rows = []
    for key, line in zip(mem_keys, mem_lines):
        twin = in_file.pop(key, None)
        rows.append(Row(fmt_time(line.time), fmt_uptime(line.ms), key[0], f"L{line.level:08x}",
                        "both" if twin else "memory", str(line.n), str(twin.n) if twin else "-", key[2]))
    if rows:  # reset time: the time of the first row of the memory log, in the order of 3.4, minus its uptime
        first = min(rows, key=sort_key)
        zero = datetime.strptime(first.time, "%Y-%m-%dT%H:%M:%S.%f") - uptime_ms(first.uptime) * MS
    elif file and file.complete and file_lines:  # END_OF_LOG clock minus the uptime of the last content line
        zero = file.end_clock - file_lines[-1].ms * MS
    else:
        zero = None
    for key, line in in_file.items():  # a file-only run: the file's text as it is
        rows.append(Row(fmt_time(zero + line.ms * MS) if zero else "-", fmt_uptime(line.ms), key[0], "-", "bootefi",
                        "-", str(line.n), norm(line.text)))
    for r in records:  # uptime of a record: its timestamp minus the run's reset time
        rows.append(Row(fmt_time(r.ts), fmt_uptime((r.ts - zero) // MS), 0, "-", "errrec", "-", "-", r.message))
    return sorted(rows, key=sort_key)


def unattached_rows(records) -> list[Row]:
    rows = [Row(fmt_time(r.ts) if r.ts else "-", "-", 0, "-", "errrec", "-", "-", r.message) for r in records]
    return sorted(rows, key=unattached_key)


def log_bytes(rows) -> bytes:
    return (COLUMNS + "".join(row.text() + "\n" for row in rows)).encode("utf-8")


FIELD_RES = [re.compile(p) for p in (
    r"-|\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}", r"-|-?\d+\.\d{3}", r"\d+", r"-|L[0-9a-f]{8}",
    r"both|bootefi|errrec|memory", r"-|[1-9]\d*", r"-|[1-9]\d*", r"[^\n]*")]
SHAPE = {"memory": (True, True, False), "both": (True, True, True), "bootefi": (False, False, True),
         "errrec": (False, False, False)}  # which of level, mem_line, file_line a source fills


def parse_log(data: bytes) -> list[Row]:
    """Read a firmware.log strictly: column line, 8 fields per row, each field in its format."""
    text = data.decode("utf-8")
    assert text.startswith(COLUMNS), f"wrong column line: {text[:120]!r}"
    assert text.endswith("\n")
    rows = []
    for line in text[len(COLUMNS):].split("\n")[:-1]:
        fields = line.split("\t")
        assert len(fields) == 8, f"row with {len(fields)} fields: {line!r}"
        for value, pattern in zip(fields, FIELD_RES):
            assert pattern.fullmatch(value), f"field {value!r} in row {line!r}"
        row = Row(fields[0], fields[1], int(fields[2]), *fields[3:])
        assert tuple(x != "-" for x in (row.level, row.mem_line, row.file_line)) == SHAPE[row.source], line
        assert row.message == norm(row.message), f"message not normalised: {line!r}"
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------------------------
# Timings and versions (the sysfs trees and the text files the keeper makes of them)

ESRT = {"entry0": ("aaaaaaaa-0000-4000-8000-000000000001", "16909060"),
        "entry1": ("aaaaaaaa-0000-4000-8000-000000000002", "4294901760")}


def fpdt_values(n: int) -> dict[str, int]:
    return {"firmware_start_ns": 300_000_000 + n, "bootloader_load_ns": 4_900_000_000 + n,
            "bootloader_launch_ns": 5_000_000_000 + n, "exitbootservice_start_ns": 9_000_000_000 + n,
            "exitbootservice_end_ns": 9_100_000_000 + n}


def write_sysfs(fpdt: Path, esrt: Path, n: int) -> None:
    fpdt.mkdir(parents=True, exist_ok=True)
    for name, value in fpdt_values(n).items():
        (fpdt / name).write_text(f"{value}\n")
    for entry, (fw_class, version) in ESRT.items():
        (esrt / entry).mkdir(parents=True, exist_ok=True)
        for name, value in (("fw_class", fw_class), ("fw_version", version), ("last_attempt_version", version),
                            ("last_attempt_status", "0"), ("capsule_flags", "0x1")):
            (esrt / entry / name).write_text(f"{value}\n")


def fpdt_text(n: int) -> str:
    return "".join(f"{name}={value}\n" for name, value in sorted(fpdt_values(n).items()))


def esrt_text() -> str:
    return "".join(f"{entry} fw_class={c} fw_version={v} last_attempt_version={v} last_attempt_status=0\n"
                   for entry, (c, v) in sorted(ESRT.items()))


def tree(root: Path) -> dict[str, bytes]:
    """Every file under root, by relative path."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()} if root.is_dir() else {}
