"""Group D of TEST-SCENARIOS.md: error records."""
import os
import uuid
from datetime import datetime, timedelta

import pytest

from builders import (COLUMNS, HWERR_GUID, LOCATOR_VAR, SUPPORT_VAR, Entry, L, MemLog, boot_lines, log_bytes, record,
                      undecodable, unattached_rows)
from conftest import not_root, read_only, writable

T0 = datetime(2026, 11, 20, 12, 0, 0)
S = timedelta(seconds=1)
MU = "85183a8b-9c41-429c-939c-5c3c087ca280"


def test_D1_new_records(machine):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    inside = record(T0 + 2 * S, 0x1A, severity=1, status=0x02010007, info1=0x10, info2=0xABCDEF0123)
    outside = record(datetime(2026, 11, 20, 9, 30, 0), 0x1B, severity=2, century=0x21,
                     other_sections=[(uuid.UUID("99999999-8888-7777-6666-555555555555"), b"\x01" * 24)])
    names = [machine.add_record(inside), machine.add_record(outside)]
    res = machine.keeper()

    assert names == [f"HwErrRec0000-{HWERR_GUID}", f"HwErrRec0001-{HWERR_GUID}"]
    # archived once, by content
    assert machine.record_copies() == {inside.hash, outside.hash}
    for rec in (inside, outside):
        assert (machine.merged_root / "hwerrrec" / f"{rec.hash}.bin").read_bytes() == rec.raw
    # decoded: one row each, in the run's log or in unattached/
    assert inside.message == (
        f"HwErrRec record_id=0x1a severity=fatal hash={inside.hash} century=20 | section type={MU} "
        "component=11111111-2222-3333-4444-555555555555 subcomponent=00000000-0000-0000-0000-000000000000 "
        "status=0x02010007 info1=0x10 info2=0xabcdef0123")
    assert f"2026-11-20T12:00:02.000\t2.000\t0\t-\terrrec\t-\t-\t{inside.message}\n" in machine.log(run).decode()
    assert machine.unattached().decode() == COLUMNS + (
        f"2026-11-20T09:30:00.000\t-\t0\t-\terrrec\t-\t-\tHwErrRec record_id=0x1b severity=corrected "
        f"hash={outside.hash} century=21 | section type={MU} component=11111111-2222-3333-4444-555555555555 "
        "subcomponent=00000000-0000-0000-0000-000000000000 status=0x02010007 info1=0x0 info2=0x0"
        " | section type=99999999-8888-7777-6666-555555555555\n")
    # reported
    for name, rec in zip(names, (inside, outside)):
        assert f"archived error record {rec.hash} from {name}" in res.lines
    assert sorted(machine.errrec_seen()) == sorted(
        (res.utc, run.boot_id, action, name, rec.hash, "-")
        for name, rec in zip(names, (inside, outside)) for action in ("archived", "deleted"))
    machine.check()


def test_D2_purge_empties_the_store(machine):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    records = [record(T0 + i * S, 0x100 + i) for i in range(11)]  # seconds 0 to 5 are inside the run, the rest after it
    names = [machine.add_record(rec) for rec in records]
    res = machine.keeper()

    assert machine.records_in_firmware() == []
    assert (machine.efivars / SUPPORT_VAR).exists()
    assert sorted(res.starting("deleted error record ")) == sorted(f"deleted error record {name}" for name in names)
    deleted = [line for line in machine.errrec_seen() if line[2] == "deleted"]
    assert sorted(deleted) == sorted((res.utc, run.boot_id, "deleted", name, rec.hash, "-") for name, rec in zip(names, records))
    assert machine.log(run) == run.log(records[:6])  # the run's rows go from 12:00:00.374 to 12:00:05.000
    assert machine.unattached() == log_bytes(unattached_rows(records[6:]))
    assert res.merged(run.boot_id) == (run.folder_name, 9, 9, 6)
    machine.check()


def test_D3_archive_copy_differs(machine):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    name = machine.add_record(rec)
    copy = machine.merged_root / "hwerrrec" / f"{rec.hash}.bin"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(rec.raw[:-1] + b"\xff")  # a damaged archive copy under the record's name
    res = machine.keeper(expect=1)

    assert machine.records_in_firmware() == [name]  # kept in the firmware
    assert (res.utc, run.boot_id, "kept", name, rec.hash, "archive-mismatch") in machine.errrec_seen()
    assert any(name in line for line in res.starting("error: "))  # reported
    assert res.starting("deleted error record ") == []
    assert copy.read_bytes() == rec.raw[:-1] + b"\xff"  # an archive file is never overwritten
    machine.check(bad_copies={rec.hash})


def test_D4_other_variables_are_never_deleted(machine):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    name = machine.add_record(rec)
    others = {
        f"HwErrRec0001-{uuid.UUID(int=7)}": record(T0 + 3 * S, 0x2B).raw,  # similar name, another identifier
        f"HwErrRecABCDE-{HWERR_GUID}": record(T0 + 3 * S, 0x2C).raw,  # not 4 hex digits
        f"HwErrRecLog-{HWERR_GUID}": b"\x07\x00\x00\x00data",
        "Boot0001-8be4df61-93ca-11d2-aa0d-00e098032b8c": b"\x07\x00\x00\x00boot entry",
    }
    for other, data in others.items():
        (machine.efivars / other).write_bytes(data)
    before = {p.name: p.read_bytes() for p in machine.efivars.iterdir()}
    res = machine.keeper()

    after = {p.name: p.read_bytes() for p in machine.efivars.iterdir()}
    assert after == {k: v for k, v in before.items() if k != name}
    assert set(after) == set(others) | {SUPPORT_VAR, LOCATOR_VAR}
    assert machine.record_copies() == {rec.hash}  # and none of them is archived as a record
    assert res.starting("deleted error record ") == [f"deleted error record {name}"]
    assert machine.log(run) == run.log([rec])
    machine.check()


@pytest.fixture
def failing_chattr(tmp_path):
    """A PATH whose chattr always fails, as on a file system without the immutable attribute."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "chattr").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "chattr").chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def test_D5_chattr_fails_deletion_still_done(machine, failing_chattr):
    machine.firmware_run(boot_lines("a"), reset=T0)
    name = machine.add_record(record(T0 + 2 * S))
    res = machine.keeper(env=failing_chattr)

    assert machine.records_in_firmware() == []
    assert f"deleted error record {name}" in res.lines
    assert res.starting("error: ") == []
    machine.check()


@not_root
def test_D5_deletion_fails(machine, failing_chattr):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    name = machine.add_record(rec)
    machine.efivars.chmod(0o555)  # the variable cannot be removed
    res = machine.keeper(expect=1, env=failing_chattr)

    assert machine.records_in_firmware() == [name]
    assert any(name in line for line in res.starting("error: "))
    assert (res.utc, run.boot_id, "kept", name, rec.hash, "delete-failed") in machine.errrec_seen()
    assert machine.log(run) == run.log([rec])  # archived and merged all the same

    machine.efivars.chmod(0o755)
    res = machine.keeper()  # next run: deleted; neither archived nor merged a second time
    assert machine.records_in_firmware() == []
    assert res.starting("archived error record ") == []
    assert machine.log(run) == run.log([rec])
    assert [line[2] for line in machine.errrec_seen()] == ["archived", "kept", "deleted"]
    machine.check()


def test_D6_same_record_written_again(machine):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    machine.keeper()
    log = machine.log(run)

    machine.firmware_run(boot_lines("b"))
    again = machine.add_record(rec, slot=3)  # the firmware wrote the same bytes again, in another slot
    res = machine.keeper()

    assert res.starting("archived error record ") == []
    assert machine.record_copies() == {rec.hash}
    assert machine.log(run) == log == run.log([rec])
    assert machine.records_in_firmware() == []
    assert f"deleted error record {again}" in res.lines
    assert [(line[2], line[4]) for line in machine.errrec_seen()] == [
        ("archived", rec.hash), ("deleted", rec.hash), ("deleted", rec.hash)]
    machine.check()


@pytest.mark.parametrize("damage", ["no signature", "cut in the header", "cut in the section table"])
def test_D7_record_that_cannot_be_decoded(machine, damage):
    good = record(T0 + 2 * S).raw
    raw = {"no signature": b"\x0f\x00\x00\x00not a CPER record at all",
           "cut in the header": good[:4 + 60], "cut in the section table": good[:4 + 150]}[damage]
    bad = undecodable(raw)
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    name = machine.add_record(bad)
    res = machine.keeper()

    assert (machine.merged_root / "hwerrrec" / f"{bad.hash}.bin").read_bytes() == raw  # archived raw
    assert f"archived error record {bad.hash} from {name}" in res.lines  # reported
    assert machine.unattached().decode() == COLUMNS + (
        f"-\t-\t0\t-\terrrec\t-\t-\tHwErrRec undecoded hash={bad.hash} length={len(raw)}\n")
    assert machine.records_in_firmware() == []  # deleted: the raw archive copy was verified
    assert machine.log(run) == run.log()
    machine.check()


def test_D8_record_inside_the_current_run(machine):
    run = machine.firmware_run([L(374, "SEC start"), L(5000, "Boot loader")], reset=T0)
    rec = record(T0 + 2 * S, 0x1A)
    machine.add_record(rec)
    res = machine.keeper()

    assert machine.log(run).decode() == COLUMNS + (
        "2026-11-20T12:00:00.374\t0.374\t0\tL80000000\tboth\t1\t1\tSEC start\n"
        f"2026-11-20T12:00:02.000\t2.000\t0\t-\terrrec\t-\t-\t{rec.message}\n"
        "2026-11-20T12:00:05.000\t5.000\t0\tL80000000\tboth\t2\t2\tBoot loader\n")
    assert machine.sources(run) == (f"memory\t{run.mem.hash}\t2\t-\t-\n"
                                    f"bootefi\t{run.file.hash}\t2\tUEFI_Log1.txt\tmerged\n"
                                    f"errrec\t{rec.hash}\t1\t-\t-\n")
    assert res.merged(run.boot_id) == (run.folder_name, 2, 2, 1)
    assert not (machine.merged_root / "unattached").exists() or machine.unattached().decode() == COLUMNS
    machine.check()


def test_D9_record_of_an_older_archived_run(machine):
    older = machine.firmware_run(boot_lines("older"), reset=T0)
    machine.keeper()
    run = machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    rec = record(T0 + 3 * S)  # found one boot later, written during the older run
    machine.add_record(rec)
    machine.keeper()

    assert machine.log(older) == older.log([rec])  # rewritten in order
    assert [r.source for r in machine.rows(older)].count("errrec") == 1
    assert machine.sources(older).endswith(f"errrec\t{rec.hash}\t1\t-\t-\n")
    assert machine.log(run) == run.log()
    assert machine.records_in_firmware() == []
    machine.check()


def test_D10_record_inside_no_run(machine):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(datetime(2026, 11, 20, 9, 30, 0), 0x1C)  # written by a run that hung in the firmware and left no log
    machine.add_record(rec)
    machine.keeper()

    assert machine.unattached().decode() == COLUMNS + f"2026-11-20T09:30:00.000\t-\t0\t-\terrrec\t-\t-\t{rec.message}\n"
    assert machine.log(run) == run.log()
    assert machine.sources(run).count("errrec") == 0
    assert machine.records_in_firmware() == []
    machine.check()


def test_D11_bounds_of_a_run_are_included(machine):
    """The run's rows go from 12:00:10.250 to 12:00:20.900: seconds 10 to 20 are inside, 9 and 21 are not."""
    reset = T0 + 10 * S
    run = machine.firmware_run([L(250, "first"), L(10900, "last")], reset=reset)
    first, last = record(reset, 0x1), record(reset + 10 * S, 0x2)
    before, after = record(reset - S, 0x3), record(reset + 11 * S, 0x4)
    for rec in (first, last, before, after):
        machine.add_record(rec)
    machine.keeper()

    assert machine.log(run) == run.log([first, last])
    assert [(r.time, r.uptime) for r in machine.rows(run) if r.source == "errrec"] == [
        ("2026-11-20T12:00:10.000", "0.000"), ("2026-11-20T12:00:20.000", "10.000")]
    assert machine.unattached() == log_bytes(unattached_rows([before, after]))
    machine.check()


@not_root
def test_D12_deleted_only_after_its_row_is_verified(machine):
    older = machine.firmware_run(boot_lines("older"), reset=T0)
    machine.keeper()
    run = machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    rec = record(T0 + 3 * S)
    name = machine.add_record(rec)
    read_only(machine.folder(older))  # its row cannot be written in the older run's log
    res = machine.keeper(expect=1)

    assert machine.records_in_firmware() == [name]
    assert res.starting("error: ") and res.starting("deleted error record ") == []
    assert (machine.merged_root / "hwerrrec" / f"{rec.hash}.bin").read_bytes() == rec.raw
    assert (res.utc, run.boot_id, "kept", name, rec.hash, "not-verified") in machine.errrec_seen()
    assert machine.log(older) == older.log()

    writable(machine.folder(older))
    res = machine.keeper()  # now the row is written and verified, then the record is deleted
    assert machine.log(older) == older.log([rec])
    assert machine.records_in_firmware() == []
    assert res.starting("archived error record ") == []

    machine.add_record(rec)  # the same record seen again is not added twice
    machine.keeper()
    assert machine.log(older) == older.log([rec])
    assert machine.records_in_firmware() == []
    machine.check()


def test_D13_overlapping_runs(machine):
    """The clock was set back between two runs: a record inside both spans goes to the run that started last."""
    lines = [L(300, "start"), L(60000, "end")]
    earlier = machine.firmware_run([L(300, "earlier start"), L(60000, "earlier end")], reset=T0)  # 12:00:00 to 12:01:00
    machine.keeper()
    run = machine.firmware_run(lines, reset=T0 - 30 * S)  # 11:59:30 to 12:00:30
    in_both = record(T0 + 15 * S, 0x1)
    in_current_only = record(T0 - 15 * S, 0x2)
    machine.add_record(in_both)
    machine.add_record(in_current_only)
    machine.keeper()

    assert machine.log(earlier) == earlier.log([in_both])
    assert machine.log(run) == run.log([in_current_only])
    machine.check()


def test_D5_immutable_attribute_removed_before_the_deletion(machine, tmp_path):
    """The variables of efivarfs are immutable: nothing can be deleted before `chattr -i <path>` has run. Here the
    fake variable can only be deleted once the chattr of PATH was called that way (it makes the folder writable)."""
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    machine.firmware_run(boot_lines("a"), reset=T0)
    name = machine.add_record(record(T0 + 2 * S))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "chattr").write_text('#!/bin/sh\n[ "$#" = 2 ] && [ "$1" = "-i" ] && chmod 755 "$(dirname "$2")"\n')
    (bin_dir / "chattr").chmod(0o755)
    machine.efivars.chmod(0o555)
    res = machine.keeper(env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})

    assert machine.records_in_firmware() == []
    assert f"deleted error record {name}" in res.lines
    machine.check()


def test_D10_unattached_record_stays_there_when_its_run_is_archived_later(machine):
    """SPEC 3.1: a record is placed once, among the runs archived when it is found."""
    machine.remove_uefilogs()
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec, outside = record(T0 + 2 * S, 0x1A), record(datetime(2026, 11, 20, 9, 30, 0), 0x1B)
    machine.add_record(rec)
    machine.add_record(outside)
    locator = (machine.efivars / LOCATOR_VAR).read_bytes()
    (machine.efivars / LOCATOR_VAR).unlink()  # the memory log cannot be read at the first keeper run
    machine.keeper()
    assert machine.unattached() == log_bytes(unattached_rows([rec, outside]))
    assert machine.records_in_firmware() == []

    (machine.efivars / LOCATOR_VAR).write_bytes(locator)
    machine.keeper()
    assert machine.log(run) == run.log()
    assert machine.unattached() == log_bytes(unattached_rows([rec, outside]))
    machine.check()  # H4: one row per record


def test_D11_record_of_the_first_second_can_be_before_the_reset(machine):
    """Reset at 12:00:10.500, first row at 12:00:10.600. A record stamped 12:00:10 is on the run's first second,
    so it is attached, half a second before the reset: its uptime is written -0.500 and its row comes first."""
    lines = [L(100, "first"), L(4500, "last")]
    entries = [Entry(x.ms * 1000, (x.text + "\r\n").encode(), x.level) for x in lines]
    mem = MemLog(entries, clock=T0 + 12 * S, clock_tick=1_500_000)
    run = machine.firmware_run(lines, memlog=mem, reset=T0 + timedelta(seconds=10.5))
    rec = record(T0 + 10 * S)
    machine.add_record(rec)
    machine.keeper()

    assert machine.log(run).decode() == COLUMNS + (
        f"2026-11-20T12:00:10.000\t-0.500\t0\t-\terrrec\t-\t-\t{rec.message}\n"
        "2026-11-20T12:00:10.600\t0.100\t0\tL80000000\tboth\t1\t1\tfirst\n"
        "2026-11-20T12:00:15.000\t4.500\t0\tL80000000\tboth\t2\t2\tlast\n")
    assert machine.log(run) == run.log([rec])
    assert machine.folder(run).name == f"2026-11-20T120010_{run.boot_id}"
    machine.keeper()  # read back and written again the same
    assert machine.log(run) == run.log([rec])
    machine.check()


def test_D11_span_of_a_log_with_two_segments(machine):
    """The span goes from the earliest to the latest time of the run's rows, wherever they are in the log: rows
    at 12:00:00.300 and 12:00:08.000 (segment 0), then 12:00:00.100 and 12:00:02.000 (segment 1)."""
    run = machine.firmware_run([L(300, "a"), L(8000, "b"), L(100, "c"), L(2000, "d")], reset=T0)
    inside, after = record(T0 + 5 * S, 0x1), record(T0 + 9 * S, 0x2)  # 12:00:05 is after the log's last row
    machine.add_record(inside)
    machine.add_record(after)
    machine.keeper()

    assert machine.log(run) == run.log([inside])
    assert [(r.seg, r.uptime, r.source) for r in machine.rows(run)] == [
        (0, "0.300", "both"), (0, "5.000", "errrec"), (0, "8.000", "both"), (1, "0.100", "both"), (1, "2.000", "both")]
    assert machine.unattached() == log_bytes(unattached_rows([after]))
    machine.check()


def test_D13_two_runs_starting_at_the_same_time(machine):
    """Both spans contain the record and start on the same millisecond: it goes to the greatest run key, compared
    as text. A `fileonly-` key is greater than any boot id."""
    hung = machine.firmware_run(boot_lines("hung"), reset=T0, linux=False)
    run = machine.firmware_run(boot_lines("now"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    machine.keeper()

    assert machine.rows(hung)[0].time == machine.rows(run)[0].time == "2026-11-20T12:00:00.374"
    assert machine.log(hung) == hung.log([rec])
    assert machine.log(run) == run.log()
    machine.check()


@pytest.mark.parametrize("swap", [False, True])
def test_D14_two_records_with_the_same_second_and_record_id(machine, swap):
    """SPEC 3.4: the record's hash is the last word on the order, whatever the order of the variables."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    records = [record(T0 + 2 * S, 0x1A, info1=1), record(T0 + 2 * S, 0x1A, info1=2)]
    for rec in records[::-1] if swap else records:
        machine.add_record(rec)
    machine.keeper()

    assert machine.log(run) == run.log(records)
    rows = [r.message for r in machine.rows(run) if r.source == "errrec"]
    assert rows == [rec.message for rec in sorted(records, key=lambda rec: rec.hash)]
    assert machine.records_in_firmware() == []
    machine.check()


@not_root
def test_D15_seen_line_that_cannot_be_written(machine):
    """The `archived` and `deleted` lines of hwerrrec/seen.tsv are the record's history: when they cannot be
    written, the record is archived and deleted all the same, and the run reports it (SPEC 4)."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    machine.add_record(record(T0 + 2 * S, 0x1))
    machine.keeper()
    seen = machine.merged_root / "hwerrrec" / "seen.tsv"
    before = seen.read_bytes()
    seen.chmod(0o444)
    rec = record(T0 + 3 * S, 0x2)
    name = machine.add_record(rec)
    res = machine.keeper(expect=1)

    assert machine.records_in_firmware() == [] and rec.hash in machine.record_copies()
    assert rec.message in machine.log(run).decode()
    assert seen.read_bytes() == before
    assert len([line for line in res.starting("error: errrec: ") if name in line]) == 2  # one per line not written
    machine.check()
