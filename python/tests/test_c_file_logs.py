"""Group C of TEST-SCENARIOS.md: damaged or unusual file logs."""
from datetime import datetime, timedelta

import pytest

from builders import COLUMNS, SLOT_SIZE, FileLog, L, boot_lines, log_bytes, merge, padding, sha16, stamp

T0 = datetime(2026, 11, 20, 12, 0, 0)


def test_C1_stale_tail_after_end_of_log(machine):
    """A short log written in a slot that held a longer one: the end of the old log stays after the new END_OF_LOG."""
    old = FileLog(boot_lines("old", n=30), T0 - timedelta(days=1))
    lines = boot_lines("new")
    new = FileLog(lines, T0 + timedelta(seconds=5)).over(old)
    run = machine.firmware_run(lines, filelog=new, reset=T0)
    res = machine.keeper()

    first_end = new.content.index(b"END_OF_LOG")
    assert new.content.count(b"END_OF_LOG") == 2 and new.trailing > 0  # what the builder made: two markers
    assert new.content.endswith(b" === END_OF_LOG === @ === 2026/11/19 12:00:00 ===")
    # the raw copy keeps the tail; only the part before the first marker is merged
    assert (machine.bootefi_root / f"{new.hash}.txt").read_bytes() == new.content
    assert machine.log(run) == run.log()
    assert {r.source for r in machine.rows(run)} == {"both"} and len(machine.rows(run)) == 9
    assert machine.folder_names() == [run.folder_name]
    # reported: the size of the tail is in seen.tsv
    tail_size = len(new.content) - new.content.index(b"\r\n", first_end)
    assert machine.bootefi_seen() == [(res.utc, run.boot_id, "UEFI_Log1.txt", new.hash, str(len(new.content)),
                                       str(tail_size), "yes", "2026/11/20 12:00:05", "1")]
    machine.check()


def test_C1_stale_tail_in_a_file_only_run(machine):
    old = FileLog(boot_lines("old", n=30), T0 - timedelta(days=1))
    hung_log = FileLog(boot_lines("hung"), T0 + timedelta(seconds=5)).over(old)
    hung = machine.firmware_run(filelog=hung_log, linux=False)
    machine.firmware_run(boot_lines("now"))
    machine.keeper()

    assert machine.log(hung) == hung.log()
    assert machine.folder(hung).name == f"2026-11-20T120000_fileonly-{sha16(hung_log.content)}"
    assert len(machine.rows(hung)) == 9
    machine.check()


def test_C2_no_end_of_log(machine):
    """An interrupted write: archived and merged all the same, recorded as incomplete."""
    lines = boot_lines("a")
    run = machine.firmware_run(lines, complete=False)
    res = machine.keeper()

    assert not run.file.complete and b"END_OF_LOG" not in run.file.content
    assert (machine.bootefi_root / f"{run.file.hash}.txt").read_bytes() == run.file.content
    assert machine.log(run) == run.log()
    assert {r.source for r in machine.rows(run)} == {"both"}
    assert machine.bootefi_seen() == [(res.utc, run.boot_id, "UEFI_Log1.txt", run.file.hash,
                                       str(len(run.file.content)), "0", "no", "-", "1")]
    machine.check()


def test_C2_write_cut_inside_a_line(machine):
    """The last line was cut while being written, the one before it has an empty message. The cut line is not in
    the memory log, so the file is another run (SPEC 3.1): kept whole in its own folder, the cut line with the
    uptime of the line before it."""
    lines = boot_lines("a") + [L(5100, "")]
    cut = FileLog(lines + [b"00:00:05.2"], None)
    run = machine.firmware_run(lines + [L(5200, "never reached the file")], filelog=cut)
    machine.keeper()

    assert cut.content.endswith(b"00:00:05.100 : \r\n00:00:05.2")
    own = f"fileonly-{cut.hash}"
    assert {r.source for r in machine.rows(run.boot_id)} == {"memory"}
    assert [(r.uptime, r.file_line, r.message) for r in machine.rows(own)[-2:]] == [
        ("5.100", "10", ""), ("5.100", "11", "00:00:05.2")]
    assert machine.log(own) == log_bytes(merge(None, cut))
    machine.check()


@pytest.mark.parametrize("pad", ["lines", "spaces", "nul", "mixed"])
def test_C3_padding_and_line_endings(machine, pad):
    """Padding of spaces, of NUL bytes, of both, or of lines of spaces; CRLF line endings. Same content each time."""
    lines = boot_lines("a")
    run = machine.firmware_run(lines, reset=T0, pad=pad)
    machine.keeper()

    data = (machine.uefilogs / "UEFI_Log1.txt").read_bytes()
    reference = FileLog(lines, T0 + timedelta(seconds=5), pad="lines")
    assert len(data) == SLOT_SIZE and run.file.content == reference.content
    assert data[len(run.file.body):] == padding(SLOT_SIZE - len(run.file.body), pad)
    assert b"\n" not in run.file.content.replace(b"\r\n", b"")
    assert machine.raw_logs() == {reference.hash}
    assert (machine.bootefi_root / f"{reference.hash}.txt").read_bytes() == reference.content
    assert machine.log(run) == run.log()
    assert {r.source for r in machine.rows(run)} == {"both"}
    machine.check()


@pytest.mark.parametrize("kind", ["empty", "lines", "spaces", "nul", "mixed"])
def test_C4_empty_file_or_only_padding(machine, kind):
    machine.write_file(b"" if kind == "empty" else padding(SLOT_SIZE, kind))
    run = machine.firmware_run(boot_lines("a"))
    res = machine.keeper()

    assert len(res.starting("skipped UEFI_Log1.txt: ")) == 1  # ignored, reported
    assert machine.raw_logs() == {run.file.hash}
    assert machine.bootefi_seen() == [machine.seen_line(res, run.file, 2, 2)]
    assert machine.folder_names() == [run.folder_name]
    assert machine.log(run) == run.log()
    machine.check()


GARBLED = [b"garbage without a time stamp", b"\xff\xfe binary \x80 bytes", b"12:34 : almost a time stamp"]


def test_C5_garbled_lines(machine):
    """Garbled lines are in no memory log: a file that has some is another run (SPEC 3.1), kept as it is."""
    lines = boot_lines("a", n=5)
    in_file = lines[:2] + GARBLED + lines[2:]
    run = machine.firmware_run(lines, file_lines=in_file, reset=T0)
    machine.keeper(expect=0)  # garbled lines never stop the run

    assert {r.source for r in machine.rows(run.boot_id)} == {"memory"}
    assert machine.log(f"fileonly-{run.file.hash}") == log_bytes(merge(None, run.file))
    assert (machine.bootefi_root / f"{run.file.hash}.txt").read_bytes() == run.file.content  # raw copy: bytes untouched
    machine.check()


def test_C5_garbled_lines_in_a_file_only_run(machine):
    in_file = boot_lines("hung", n=2)[:2] + GARBLED + [L(5000, "hung boot loader")]
    hung = machine.firmware_run(file_lines=in_file, linux=False, reset=T0)
    machine.firmware_run(boot_lines("now"))
    machine.keeper(expect=0)  # garbled lines never stop the run

    assert machine.log(hung).decode() == COLUMNS + (
        "2026-11-20T12:00:00.374\t0.374\t0\t-\tbootefi\t-\t1\thung step 0\n"
        "2026-11-20T12:00:00.511\t0.511\t0\t-\tbootefi\t-\t2\thung step 1\n"
        "2026-11-20T12:00:00.511\t0.511\t0\t-\tbootefi\t-\t3\tgarbage without a time stamp\n"
        "2026-11-20T12:00:00.511\t0.511\t0\t-\tbootefi\t-\t4\tÿþ binary \u0080 bytes\n"
        "2026-11-20T12:00:00.511\t0.511\t0\t-\tbootefi\t-\t5\t12:34 : almost a time stamp\n"
        "2026-11-20T12:00:05.000\t5.000\t0\t-\tbootefi\t-\t6\thung boot loader\n")
    machine.check()


def test_C5_line_ended_by_a_line_feed_alone(machine):
    """SPEC 1.2: a line ends at LF, with or without a CR before it."""
    bare = b"00:00:00.600 : ended by a line feed alone\n00:00:00.700 : the line after it"
    in_file = boot_lines("hung", n=2)[:2] + [bare, L(5000, "hung boot loader")]
    hung = machine.firmware_run(file_lines=in_file, linux=False, reset=T0)
    machine.firmware_run(boot_lines("now"))
    machine.keeper()

    assert machine.log(hung).decode() == COLUMNS + (
        "2026-11-20T12:00:00.374\t0.374\t0\t-\tbootefi\t-\t1\thung step 0\n"
        "2026-11-20T12:00:00.511\t0.511\t0\t-\tbootefi\t-\t2\thung step 1\n"
        "2026-11-20T12:00:00.600\t0.600\t0\t-\tbootefi\t-\t3\tended by a line feed alone\n"
        "2026-11-20T12:00:00.700\t0.700\t0\t-\tbootefi\t-\t4\tthe line after it\n"
        "2026-11-20T12:00:05.000\t5.000\t0\t-\tbootefi\t-\t5\thung boot loader\n")
    machine.check()


@pytest.mark.parametrize("clock, last", [("99999999999/11/20 12:00:05", 5000),  # no such year
                                         ("1/1/1 0:00:05", 100 * 3_600_000 - 1)])  # reset time before the year 1
def test_C5_end_of_log_clock_outside_the_calendar(machine, clock, last):
    """SPEC 1.2: such a clock counts as no clock. The file is archived and merged like one without END_OF_LOG
    time, at every keeper run."""
    lines = boot_lines("hung", n=3)[:3] + [L(last, "hung boot loader")]
    body = b"".join(f"{stamp(x.ms)} : {x.text}\r\n".encode() for x in lines)
    content = body + b"\r\n\r\n === END_OF_LOG === @ === " + clock.encode() + b" ==="
    machine.write_file(content + b"\r\n\r\n" + b" " * 300)
    run = machine.firmware_run(boot_lines("now"))
    for _ in range(2):
        res = machine.keeper()
        assert res.starting("error: ") == []

    rows = machine.rows(f"fileonly-{sha16(content)}")
    assert [(r.time, r.uptime, r.message) for r in rows] == [("-", f"{x.ms // 1000}.{x.ms % 1000:03d}", x.text) for x in lines]
    assert machine.bootefi_seen()[0][3:8] == (sha16(content), str(len(content)), "0", "yes", clock)
    assert machine.log(run) == run.log()
    machine.check()
