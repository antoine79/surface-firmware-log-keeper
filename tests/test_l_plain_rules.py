"""Group L of TEST-SCENARIOS.md: damaged or hostile input. The keeper never panics, names the step that failed,
exits 1 and deletes nothing it should not."""
import os
import subprocess
from datetime import datetime, timedelta

import pytest

from builders import boot_lines, record, tree
from conftest import keeper_bin, not_root

T0 = datetime(2026, 11, 20, 12, 0, 0)


def test_L1_memory_line_count_of_a_run_folder(machine):
    """sources.tsv damaged from outside: a count that is not a number stops that run, which keeps its folder."""
    boot = "ab" * 16
    folder = machine.merged_root / f"2026-11-21T100000_{boot}"
    folder.mkdir(parents=True)
    (folder / "sources.tsv").write_text(f"memory\t{'0' * 16}\tmany\t-\t-\n")
    before = tree(folder)
    machine.remove_uefilogs()
    res = machine.keeper(expect=1, boot_id=machine.new_boot_id())

    assert [line for line in res.starting("error: merge: ") if folder.name in line]
    assert tree(folder) == before
    folder.joinpath("sources.tsv").unlink()
    folder.rmdir()
    machine.check()


def test_L1_damaged_unattached_log_keeps_the_record_in_the_firmware(machine):
    """unattached/firmware.log damaged from outside while the firmware still holds the record of its row: the log
    cannot be read, so the record stays in the firmware."""
    machine.remove_uefilogs()
    rec = record(datetime(2026, 3, 4, 5, 6, 7))
    machine.add_record(rec)
    boot = machine.new_boot_id()
    machine.keeper(boot_id=boot)
    path = machine.merged_root / "unattached" / "firmware.log"
    log = path.read_bytes()
    path.write_bytes(log.replace(b"2026-03-04T05:06:07.000", b"2026-03-04T05:06:07"))
    name = machine.add_record(rec)
    res = machine.keeper(expect=1, boot_id=boot)

    assert [line for line in res.starting("error: errrec: ") if name in line]
    assert machine.records_in_firmware() == [name]
    path.write_bytes(log)
    machine.check()


@pytest.mark.parametrize("now", ["2026-11-21t05:00:00z", "2026-11-21T05:00:60Z"])
def test_L1_fwlk_now_has_one_format(machine, now):
    machine.firmware_run(boot_lines("a"))
    res = machine.keeper(expect=1, env={"FWLK_NOW": now})

    assert len(res.lines) == 1 and res.starting("error: keeper: ")
    assert not machine.merged_root.exists()
    machine.check()


def test_L1_a_setting_that_is_not_utf8_is_refused(machine):
    machine.remove_uefilogs()
    root = machine.root / os.fsdecode(b"boot\xffefi")
    res = machine.keeper(expect=1, boot_id=machine.new_boot_id(), env={"FWLK_BOOTEFI_ROOT": str(root)})

    assert len(res.lines) == 1 and res.starting("error: keeper: ")
    assert not root.exists() and not machine.merged_root.exists() and not machine.memory_root.exists()
    machine.check()


@not_root
def test_L2_staging_folder_that_cannot_be_removed(machine):
    crashed = machine.firmware_run(boot_lines("crashed"), reset=T0)
    staging = machine.stage(crashed, datetime(2026, 11, 21, 4, 0, 0))
    (staging / "sub").mkdir()
    (staging / "sub" / "f").write_bytes(b"x")
    (staging / "sub").chmod(0o555)
    run = machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    res = machine.keeper(expect=1)

    assert res.starting("error: purge: ")
    assert machine.log(crashed) == crashed.log() and machine.log(run) == run.log()
    (staging / "sub").chmod(0o755)
    machine.check()


def test_L2_two_staging_folders_of_one_boot_with_other_timings(machine):
    """The run keeps the timings of the later folder. The earlier one holds timings no run folder has: it stays."""
    crashed = machine.firmware_run(boot_lines("crashed"), reset=T0)
    early = machine.stage(crashed, datetime(2026, 11, 21, 4, 0, 0))
    machine.stage(crashed, datetime(2026, 11, 21, 5, 0, 0))
    (early / "fpdt-boot.txt").write_text("other=1\n")
    machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    res = machine.keeper(expect=1)

    assert [line for line in res.starting("error: memory: ") if str(early) in line]
    assert list(machine.memory_root.iterdir()) == [early]
    assert (early / "fpdt-boot.txt").read_text() == "other=1\n"
    machine.check()


@not_root
def test_L2_merged_root_that_cannot_be_listed(machine):
    """Nothing is merged into an archive the keeper cannot read, but the file logs still get their raw copies, and
    nothing is purged."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    name = machine.add_record(record(T0 + timedelta(seconds=2)))
    machine.merged_root.mkdir(parents=True)
    machine.merged_root.chmod(0o333)
    res = machine.keeper(expect=1)
    machine.merged_root.chmod(0o755)

    assert res.starting("error: keeper: ") and res.starting("archived file log ")
    assert machine.records_in_firmware() == [name]
    assert len(list(machine.memory_root.iterdir())) == 1
    machine.keeper()
    assert machine.records_in_firmware() == [] and machine.log(run)
    machine.check()


def test_L2_slots_that_are_folders(machine):
    for slot in ("UEFI_Log1.txt", "UEFI_Log2.txt"):
        (machine.uefilogs / slot).mkdir()
    esp = tree(machine.uefilogs)
    res = machine.keeper(expect=1, boot_id=machine.new_boot_id())

    assert sorted(line.split(": ")[1] for line in res.starting("error: ")) == ["UEFI_Log1.txt", "UEFI_Log2.txt"]
    assert tree(machine.uefilogs) == esp
    machine.check()


@pytest.mark.parametrize("args, rc", [((), 0), (("bogus",), 2)])
def test_L3_output_to_a_closed_pipe_is_dropped_and_the_run_goes_on(machine, args, rc):
    run = machine.firmware_run(boot_lines("a"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("FWLK_")}
    env.update(machine.env(run.boot_id, machine.now))
    machine.now += timedelta(minutes=1)
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        p = subprocess.run([*keeper_bin(), *args], stdout=write_end, stderr=subprocess.PIPE, env=env, timeout=600)
    finally:
        os.close(write_end)

    assert (p.returncode, p.stderr) == (rc, b"")
    if rc == 0:
        machine.memory_seen[run.boot_id] = run.mem
        assert machine.log(run) == run.log()
        machine.keeper()  # nothing left to do
        assert machine.log(run) == run.log()
    machine.check()
