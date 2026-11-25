"""Group K of TEST-SCENARIOS.md: the report program reads the archive and writes nothing."""
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from builders import L, boot_lines, record, tree
from conftest import keeper_bin

T0 = datetime(2026, 11, 20, 12, 0, 0)


def report_bin() -> str:
    """FWLK_REPORT_BIN, else the report built beside the keeper."""
    exe = Path(os.environ.get("FWLK_REPORT_BIN") or Path(keeper_bin()[0]).with_name("firmware-log-report"))
    if not exe.is_file():
        pytest.fail(f"report executable not found: {exe} (set FWLK_REPORT_BIN, or build rust/)", pytrace=False)
    return str(exe.resolve())


def report(machine, *args, expect: int = 0) -> str:
    """Run the report on the machine's archive; check its exit status and that no file changed (K5)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("FWLK_")}
    env.update({"FWLK_MEMORY_ROOT": str(machine.memory_root), "FWLK_BOOTEFI_ROOT": str(machine.bootefi_root),
                "FWLK_MERGED_ROOT": str(machine.merged_root)})
    before = tree(machine.root)
    p = subprocess.run([report_bin(), *map(str, args)], env=env, capture_output=True, timeout=60)
    assert tree(machine.root) == before, "the report changed a file"
    assert p.returncode == expect, p.stdout.decode() + p.stderr.decode()
    return p.stdout.decode()


def section(out: str, title: str) -> list[str]:
    """The lines of the section whose heading starts with title, heading included."""
    block = next(b for b in out.split("\n\n") if b.startswith(title))
    return block.splitlines()


def test_K1_runs_with_sources_rows_and_stage(machine):
    first = machine.firmware_run(boot_lines("a"), late=[L(7000, "a after the hand-over")], reset=T0)
    machine.keeper()
    second = machine.firmware_run(boot_lines("b"), write_file=False)
    machine.keeper()
    out = report(machine)

    assert out.startswith("firmware runs: 2 (2 reached Linux, 0 known by their file log only)\n")
    one = next(line for line in out.splitlines() if line.startswith(first.folder_name)).split()
    # memory and one file; 9 lines in both copies, 1 in memory only; hand-over at the file's last line, 5.000 s
    assert one[1:] == ["memory,", "1", "file", "both", "9", "memory", "1", "1", "5.000"]
    two = next(line for line in out.splitlines() if line.startswith(second.folder_name)).split()
    # no file log: nothing to compare, and the stage comes from the boot timings (bootloader_launch_ns)
    assert two[1:] == ["memory", "memory", "9", "-", "5.000"]
    machine.check()


def test_K2_rotation_gap_and_runs_that_never_reached_linux(machine):
    """B7's archive, after an earlier keeper run: 13 file logs written between the two keeper runs, 9 slots."""
    machine.firmware_run(boot_lines("before"))
    first = machine.keeper()
    hung = [machine.firmware_run(boot_lines(f"hung{i}"), linux=False) for i in range(12)]
    machine.firmware_run(boot_lines("now"))
    second = machine.keeper()
    out = report(machine)

    assert section(out, "file logs overwritten") == [
        "file logs overwritten before a keeper run saw them:",
        f"  between the keeper runs {first.utc} and {second.utc}: all 9 file logs were new, 4 or more were "
        "overwritten unseen"]
    lines = section(out, "runs known by their file log only")
    assert lines[0] == "runs known by their file log only: 8"
    for run in hung[4:]:
        assert f"  {run.folder_name}  never reached a Linux boot that ran the keeper (a hang in the firmware, or " \
               f"another system started); first seen {second.utc}" in lines
    machine.check()


def test_K2_no_gap_and_a_file_older_than_the_keeper(machine):
    hung = machine.firmware_run(boot_lines("hung"), linux=False)
    machine.firmware_run(boot_lines("a"))
    first = machine.keeper()
    machine.firmware_run(boot_lines("b"))
    machine.keeper()
    out = report(machine)

    assert section(out, "file logs overwritten") == ["file logs overwritten before a keeper run saw them: none"]
    assert section(out, "runs known by their file log only") == [
        "runs known by their file log only: 1",
        f"  {hung.folder_name}  first seen by the first keeper run ({first.utc}): written before the keeper ran, it "
        "may have reached Linux"]
    machine.check()


def test_K3_long_stages_and_repeated_lines_above_the_thresholds(machine):
    slow = [L(374, "slow start")] + [L(1000 + i, "XhcStopEndpoint: Time out") for i in range(60)] + [L(40000, "slow end")]
    slow_run = machine.firmware_run(slow, reset=T0)
    machine.keeper()
    machine.firmware_run(boot_lines("fast"))
    machine.keeper()

    out = report(machine)  # defaults: over 30 s, more than 50 times
    assert section(out, "firmware stages over 30 s") == ["firmware stages over 30 s: 1",
                                                         f"  {slow_run.folder_name}  40.000 s"]
    assert section(out, "lines repeated more than 50 times") == [
        "lines repeated more than 50 times in one run:", f"  {slow_run.folder_name}  60x  XhcStopEndpoint: Time out"]

    out = report(machine, "--stage-over", "50", "--repeat-over", "60")
    assert section(out, "firmware stages over 50 s") == ["firmware stages over 50 s: 0"]
    assert section(out, "lines repeated more than 60 times") == ["lines repeated more than 60 times in one run:",
                                                                 "  none"]
    machine.check()


def test_K4_error_records_with_when_archived_and_deleted(machine):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    inside, outside = record(T0 + timedelta(seconds=2), 0x30), record(T0 + timedelta(hours=5), 0x31)
    machine.add_record(inside)
    machine.add_record(outside)
    res = machine.keeper()
    out = report(machine)

    lines = section(out, "error records")
    assert lines[0] == "error records: 2 (1 attached to no run)"
    assert lines[1].startswith(f"  2026-11-20T12:00:02.000  archived {res.utc}, deleted {res.utc}  in "
                               f"{run.folder_name}  HwErrRec record_id=0x30 severity=fatal hash={inside.hash}")
    assert lines[2].startswith(f"  2026-11-20T17:00:00.000  archived {res.utc}, deleted {res.utc}  in unattached  "
                               f"HwErrRec record_id=0x31 severity=fatal hash={outside.hash}")
    machine.check()


def test_K5_timeline_and_decode_advlog_write_nothing(machine, tmp_path):
    a = machine.firmware_run(boot_lines("a"), reset=T0)
    machine.add_record(record(T0 + timedelta(hours=5), 0x31))
    machine.keeper()
    b = machine.firmware_run(boot_lines("b"), reset=T0 + timedelta(hours=1))
    dump = tmp_path / "advlog.bin"
    dump.write_bytes(b.mem.buffer())
    machine.keeper()

    rows = report(machine, "timeline").splitlines()
    assert rows[0] == "time\tuptime_s\tsource\trun\tmessage"
    keys = [r.split("\t")[3] for r in rows[1:]]
    assert keys == [a.boot_id] * 9 + [b.boot_id] * 9 + ["unattached"]  # by firmware-clock time
    times = [r.split("\t")[0] for r in rows[1:]]
    assert times == sorted(times)

    decoded = report(machine, "decode-advlog", dump).splitlines()
    archived = [r.split("\t") for r in machine.log(b).decode().splitlines()[1:]]
    assert decoded == ["time\tuptime_s\tlevel\tmessage"] + ["\t".join((r[0], r[1], r[3], r[7])) for r in archived]
    machine.check()


@pytest.mark.parametrize("args", [("--stage-over",), ("--stage-over", "-1"), ("--repeat-over", "x"), ("report",),
                                  ("decode-advlog",), ("timeline", "x")])
def test_K5_usage_errors(machine, args):
    machine.firmware_run(boot_lines("a"))
    machine.keeper()
    assert report(machine, *args, expect=2).startswith("usage: ")
    machine.check()


def test_K5_missing_archive_is_an_error(machine):
    out = report(machine, expect=1)
    assert out.startswith("firmware runs: 0 ")
    assert f"error: {machine.merged_root}: " in out


def test_K5_reader_that_goes_away(machine):
    """`firmware-log-report | head -1`: the report stops when nobody reads its output, without a crash message."""
    for tag in "abcdefghij":
        machine.firmware_run(boot_lines(tag))
        machine.keeper()
    env = {k: v for k, v in os.environ.items() if not k.startswith("FWLK_")}
    env["FWLK_MERGED_ROOT"], env["FWLK_BOOTEFI_ROOT"] = str(machine.merged_root), str(machine.bootefi_root)
    p = subprocess.Popen([report_bin(), "timeline"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdout.readline()
    p.stdout.close()
    assert p.wait(timeout=60) in (0, 1) and p.stderr.read() == b""
    machine.check()
