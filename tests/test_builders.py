"""Checks of the test builders themselves: they need no keeper. The expected merge must agree with rows written out
by hand."""
from datetime import datetime, timedelta

from pathlib import Path

import pytest

from builders import COLUMNS, SLOT_SIZE, Entry, FileLog, L, MemLog, boot_lines, log_bytes, merge, parse_log, record

T0 = datetime(2026, 11, 20, 12, 0, 0)


MIXED = [Entry(374_000, b"one\r\n"), Entry(500_000, b"new style\r\n", 0x40, "ALM2"),
         Entry(600_000, b"two\r\nlines\r\n", 0x80000004, "ALM2"), Entry(700_000, b"split "), Entry(700_400, b"line\r\n"),
         Entry(800_000, b"no line break")]


def test_memory_lines_take_the_entry_that_ends_them():
    mem = MemLog(MIXED, clock=T0 + timedelta(seconds=2))
    assert [(x.n, x.ms, x.level) for x in mem.lines()][4] == (5, 700, 0x80000000)
    assert mem.reset == T0 and mem.lines()[0].time == T0 + timedelta(milliseconds=374)


def test_file_log_layout():
    lines = [L(374, "SEC start"), L(376, ""), L(5000, "Boot loader")]
    log = FileLog(lines, T0 + timedelta(seconds=5))
    data = log.file_bytes()
    assert len(data) == SLOT_SIZE
    assert data.startswith(b"00:00:00.374 : SEC start\r\n00:00:00.376 : \r\n00:00:05.000 : Boot loader\r\n\r\n\r\n"
                           b" === END_OF_LOG === @ === 2026/11/20 12:00:05 ===\r\n\r\n" + b" " * 70 + b"\r\n")
    assert log.trailing == 0 and log.complete
    assert [(x.n, x.ms, x.text) for x in log.rows()] == [(1, 374, "SEC start"), (2, 376, ""), (3, 5000, "Boot loader")]
    older = FileLog(boot_lines("old", n=30), T0 - timedelta(days=1))
    stale = log.over(older)
    assert stale.content.startswith(log.content + b"\r\n\r\n") and stale.trailing == len(stale.content) - len(log.content)
    assert [x.text for x in stale.rows()] == ["SEC start", "", "Boot loader"]


def test_expected_merge_agrees_with_rows_written_by_hand():
    in_memory = [L(374, "SEC start"), L(1000, "Driver A loaded", 0x80000004), L(1000, "dup"), L(1000, "dup"), L(5000, "end")]
    in_file = [L(374, "SEC start"), L(1000, "dup"), L(5000, "end")]
    rec = record(T0 + timedelta(seconds=1), 0x1A)
    rows = merge(MemLog.from_lines(in_memory, T0), FileLog(in_file, T0 + timedelta(seconds=5)), [rec])
    expected = COLUMNS + (
        "2026-11-20T12:00:00.374\t0.374\t0\tL80000000\tboth\t1\t1\tSEC start\n"
        "2026-11-20T12:00:01.000\t1.000\t0\tL80000000\tboth\t3\t2\tdup\n"
        f"2026-11-20T12:00:01.000\t1.000\t0\t-\terrrec\t-\t-\t{rec.message}\n"
        "2026-11-20T12:00:01.000\t1.000\t0\tL80000004\tmemory\t2\t-\tDriver A loaded\n"
        "2026-11-20T12:00:01.000\t1.000\t0\tL80000000\tmemory\t4\t-\tdup\n"
        "2026-11-20T12:00:05.000\t5.000\t0\tL80000000\tboth\t5\t3\tend\n")
    assert log_bytes(rows).decode() == expected
    assert parse_log(expected.encode()) == rows
    garbled = in_file[:1] + [L(1000, "Driver B loaded"), b"garbled"] + in_file[1:]
    with pytest.raises(AssertionError):  # a file with a line the memory log lacks is another run
        merge(MemLog.from_lines(in_memory, T0), FileLog(garbled, T0 + timedelta(seconds=5)))
    file_only = merge(None, FileLog(garbled, T0 + timedelta(seconds=5)))
    assert [(r.time, r.source, r.file_line, r.message) for r in file_only][1:3] == [
        ("2026-11-20T12:00:01.000", "bootefi", "2", "Driver B loaded"),
        ("2026-11-20T12:00:01.000", "bootefi", "3", "garbled")]
    assert {r.time for r in merge(None, FileLog(garbled, None))} == {"-"}


def test_keeper_bin_is_an_absolute_path(monkeypatch):
    """A bare file name would be looked up in PATH by the system, where another program may have that name."""
    from conftest import keeper_bin
    exe = Path(keeper_bin()[0])  # the keeper under test: FWLK_BIN, or the default build
    monkeypatch.chdir(exe.parent)
    monkeypatch.setenv("FWLK_BIN", exe.name)
    assert keeper_bin() == [str(exe)]


def test_expected_merge_of_a_tagged_file_log():
    lines = [L(374, "SEC start", 0x40), L(5000, "Boot loader", 0x80000000)]
    mem = MemLog.from_lines(lines, T0)
    tagged = FileLog(lines, T0 + timedelta(seconds=5), phase="[DXE  ] ")
    assert [(r.source, r.message) for r in merge(mem, tagged)] == [("both", "SEC start"), ("both", "Boot loader")]
    assert merge(mem, tagged)[0].text() == "2026-11-20T12:00:00.374\t0.374\t0\tL00000040\tboth\t1\t1\tSEC start"
    with pytest.raises(AssertionError):
        merge(mem, FileLog(lines + [L(5000, "file only", 0x2)], T0 + timedelta(seconds=5), phase="[DXE  ] "))
