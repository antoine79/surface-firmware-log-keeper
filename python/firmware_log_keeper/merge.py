"""The merge of SPEC section 3: row keys, the rows of one run's firmware.log, their order, and the check of 3.5.

A row is a list [seg, uptime ms, source, number, hash, time, level, mem_line, file_line, message]: its first five
fields are the order of 3.4 (number is the line number, or the record id of an error record, whose hash follows).
"""
import re
from datetime import datetime, timedelta

COLUMNS = "time\tuptime_s\tseg\tlevel\tsource\tmem_line\tfile_line\tmessage\n"
MS = timedelta(milliseconds=1)
SEG, UPTIME, SOURCE, NUMBER, HASH, TIME, LEVEL, MEM_LINE, FILE_LINE, MESSAGE = range(10)
RECORD_HASH = re.compile(r"hash=([0-9a-f]{16})")
RECORD_ID = re.compile(r"record_id=0x([0-9a-f]+)")
TIME_FIELD = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{3})")
UPTIME_FIELD = re.compile(r"(-?)([0-9]+)\.([0-9]{3})")


def number(text):
    """A number the keeper reads (SPEC 2): ASCII digits only, and below 2**63."""
    if not re.fullmatch("[0-9]+", text) or int(text) >= 2**63:
        raise ValueError(f"{text!r} is not a number of 64 bits")
    return int(text)


def norm(text):
    """The message column: trailing spaces, tabs, CR and LF removed, tabs replaced by spaces."""
    return text.rstrip(" \t\r\n").replace("\t", " ")


def keys(lines):
    """Row keys (3.3) of the (uptime ms, message) lines of one source: (segment, uptime ms, message, occurrence)."""
    seg, prev, count, out = 0, None, {}, []
    for ms, message in lines:
        if prev is not None and ms < prev:
            seg += 1
        prev = ms
        key = (seg, ms, message)
        count[key] = count.get(key, 0) + 1
        out.append(key + (count[key],))
    return out


def mem_keys(mem):
    return keys((line[0], line[3]) for line in mem or ())


# The tags newer Project Mu firmware writes before each line of the file log, and only there: a boot phase
# ("[DXE  ] ", or "[0004] " for a phase it has no name for), then a debug level ("[INFO] ").
TAGS = re.compile(r"(?:(?:\[[^\]]{5}\]|\[[0-9A-Fa-f]{4}\])(?: |$))?(?:\[[^\]]{4}\](?: |$))?")


def untag(message):
    return message[TAGS.match(message).end():]


def file_keys(file, tagged=False):
    return keys((ms, untag(message) if tagged else message) for _, ms, message in file.lines)


def match(file, in_mem):
    """(row keys, tagged) of a file log if every one of its keys is among in_mem, the set of a memory log's keys
    (3.1), else None. The text is compared as it is, then, if that is not complete, without the firmware's tags."""
    for tagged in (False, True):
        found = file_keys(file, tagged)
        if in_mem.issuperset(found):
            return found, tagged
    return None


def pair(mem, files):
    """(keys of the memory log, {hash: match()} of the file logs, hash of the file log merged).

    The file merged is the longest of those the memory log holds, the lowest hash if several are as long; a
    file-only run (mem None) merges its file as it is."""
    in_mem = mem_keys(mem)
    matched = {h: (file_keys(f), False) if mem is None else match(f, set(in_mem)) for h, f in files.items()}
    return in_mem, matched, min((h for h in files if matched[h]), key=lambda h: (-len(matched[h][0]), h), default=None)


def begins(log, start):
    """Is `start` the beginning of the memory log `log`: same uptime and text, line by line? The last line of
    `start` may be the unfinished form of its line in `log` (a dump taken before the firmware ended that line)."""
    n = len(start)
    if n > len(log):
        return False
    a, b = [(x[0], x[3]) for x in start], [(x[0], x[3]) for x in log[:n]]
    return a[:n - 1] == b[:n - 1] and (n == 0 or a[-1] == b[-1] or a[-1][0] <= b[-1][0] and b[-1][1].startswith(a[-1][1]))


def render(mem, files, records):
    """The rows of one run, in order, the hash of the file log merged, and whether that file was read as tagged.

    mem: lines of the memory log (advlog.lines) or None; files: {hash: FileLog} of every file log of the run;
    records: {hash: (timestamp, record id, message)} of its error records.
    """
    in_mem, matched, merged = pair(mem, files)
    twins = dict(zip(matched[merged][0], files[merged].lines)) if merged else {}
    rows = []
    for n, (key, (ms, time, level, message)) in enumerate(zip(in_mem, mem or ()), 1):
        twin = twins.pop(key, None)
        rows.append([key[0], ms, "both" if twin else "memory", n, "", time, level, n, twin and twin[0], message])
    for key, (n, ms, message) in twins.items():  # a file-only run: the file's text as it is
        rows.append([key[0], ms, "bootefi", n, "", None, "-", None, n, message])
    rows.sort(key=lambda r: r[:5])
    # reset time: the time of the first row of the memory log minus its uptime; a file-only run, whose rows are
    # all bootefi, has the END_OF_LOG clock instead and its rows are timed from it
    reset, times = None, []
    try:
        if mem:
            reset = rows[0][TIME] and rows[0][TIME] - rows[0][UPTIME] * MS
        elif merged and files[merged].end_clock and files[merged].lines:
            reset = files[merged].end_clock - files[merged].lines[-1][1] * MS
            times = [reset + r[UPTIME] * MS for r in rows]
    except OverflowError:  # a time outside the years 1 to 9999: the run has no reset time (1.2)
        reset, times = None, []
    for r, time in zip(rows, times):
        r[TIME] = time
    if records and not reset:
        raise ValueError("error records in a run without firmware-clock time")
    for h, (time, rec_id, message) in records.items():  # a record of the run's first second can be before the reset
        rows.append([0, (time - reset) // MS, "errrec", rec_id, h, time, "-", None, None, message])
    rows.sort(key=lambda r: r[:5])
    return rows, merged, bool(merged) and matched[merged][1]


def span(rows):
    """(earliest, latest) firmware-clock times of a run's log lines, or None for a run without time."""
    times = [r[TIME] for r in rows if r[SOURCE] != "errrec" and r[TIME]]
    return (min(times), max(times)) if times else None


def unattached(records):
    """The rows of unattached/firmware.log, in its order: time, record id, hash."""
    rows = [[0, None, "errrec", rec_id, h, time, "-", None, None, message]
            for h, (time, rec_id, message) in records.items()]
    return sorted(rows, key=lambda r: (fmt_time(r[TIME]), r[NUMBER], r[HASH]))


def fmt_time(t):
    if t is None:
        return "-"
    return (f"{t.year:04d}-{t.month:02d}-{t.day:02d}T{t.hour:02d}:{t.minute:02d}:{t.second:02d}"
            f".{t.microsecond // 1000:03d}")


def fmt_uptime(ms):
    if ms is None:
        return "-"
    return f"{'-' if ms < 0 else ''}{abs(ms) // 1000}.{abs(ms) % 1000:03d}"


def log_bytes(rows):
    out = [COLUMNS]
    for r in rows:
        out.append("\t".join((fmt_time(r[TIME]), fmt_uptime(r[UPTIME]), str(r[SEG]), r[LEVEL], r[SOURCE],
                              str(r[MEM_LINE] or "-"), str(r[FILE_LINE] or "-"), r[MESSAGE])) + "\n")
    return "".join(out).encode("utf-8")


def parse(data):
    """The rows of a firmware.log, as lists of the same shape."""
    text = data.decode("utf-8")
    if not text.startswith(COLUMNS) or not text.endswith("\n"):
        raise ValueError("not a firmware.log")
    rows = []
    for line in text[len(COLUMNS):].split("\n")[:-1]:
        f = line.split("\t")
        if len(f) != 8:
            raise ValueError(f"row with {len(f)} fields")
        t, up = TIME_FIELD.fullmatch(f[0]), UPTIME_FIELD.fullmatch(f[1])
        if f[0] != "-" and not t or f[1] != "-" and not up:
            raise ValueError(f"time {f[0]!r} or uptime {f[1]!r} is not one")
        time = t and datetime(*map(int, t.groups()[:6]), int(t[7]) * 1000)
        ms = up and number(up[2] + up[3]) * (-1 if up[1] else 1)
        mem_line, file_line = (None if x == "-" else number(x) for x in f[5:7])
        if mem_line and ms is None:  # it could not be compared with a dump
            raise ValueError(f"memory line {mem_line} without uptime")
        order, h = mem_line or file_line, ""
        if f[4] == "errrec":
            found, rec_id = RECORD_HASH.search(f[7]), RECORD_ID.search(f[7])
            order, h = int(rec_id[1], 16) if rec_id else 0, found[1] if found else ""
            if order >= 2**64:
                raise ValueError(f"record id {rec_id[1]} beyond 64 bits")
        rows.append([number(f[2]), ms, f[4], order, h, time, f[3], mem_line, file_line, f[7]])
    return rows


def memory_lines(rows):
    """The memory log's lines back from the rows of a firmware.log, in line order."""
    kept = sorted((r for r in rows if r[MEM_LINE]), key=lambda r: r[MEM_LINE])
    return [(r[UPTIME], r[TIME], r[LEVEL], r[MESSAGE]) for r in kept]


def keeps(data, old):
    """Does a candidate firmware.log hold what the log it replaces is the only copy of: every memory line (the
    last one may have been unfinished then) and every error record? Raises ValueError otherwise."""
    new, was = parse(data), parse(old)
    if not begins(memory_lines(new), memory_lines(was)):
        raise ValueError("the log it would replace has memory lines it lacks")
    lost = {r[HASH] for r in was if r[SOURCE] == "errrec"} - {r[HASH] for r in new if r[SOURCE] == "errrec"}
    if lost:
        raise ValueError(f"the log it would replace has an error record it lacks ({min(lost)})")


def verify(data, mem, file, records, tagged=False):
    """The check of 3.5 on a candidate firmware.log read back: every line number of every merged source and every
    record appears exactly once, with the same uptime and text as in the source. Raises ValueError otherwise.

    tagged: the file log was paired without its tags; its rows then hold the text of the memory copy."""
    got_mem, got_file, got_rec = {}, {}, {}
    for r in parse(data):
        if r[MEM_LINE]:
            got_mem.setdefault(r[MEM_LINE], []).append((r[UPTIME], r[MESSAGE]))
        if r[FILE_LINE]:
            got_file.setdefault(r[FILE_LINE], []).append((r[UPTIME], r[MESSAGE], r[SOURCE] == "both"))
        if r[SOURCE] == "errrec":
            got_rec.setdefault(r[HASH], []).append(r[MESSAGE])
    both = mem is not None  # a file merged with a memory log is all in it (3.1): every one of its rows is `both`
    want_file = {n: [(ms, untag(message) if tagged else message, both)] for n, ms, message in (file.lines if file else ())}
    for name, got, want in (
            ("memory", got_mem, {n: [(line[0], line[3])] for n, line in enumerate(mem or (), 1)}),
            ("bootefi", got_file, want_file),
            ("errrec", got_rec, {h: [message] for h, (_, _, message) in records.items()})):
        if got != want:
            odd = sorted(set(got) ^ set(want) or (k for k in want if got[k] != want[k]), key=str)
            raise ValueError(f"{name} rows differ from the source (first: {odd[0]})")
