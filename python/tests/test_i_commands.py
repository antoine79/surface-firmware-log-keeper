"""Group I of TEST-SCENARIOS.md: the keeper has one command, a run; the Python reference also has `migrate`."""
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from builders import boot_lines, record, tree
from conftest import DEFAULT_BIN


@pytest.mark.parametrize("args", [("decode-cper", "x.bin"), ("decode-advlog", "advlog.bin"), ("status",), ("run",),
                                  ("report",), ("migrate",), ("migrate", "a", "b"), ("migrate", "")])
def test_I1_no_other_command(machine, args):
    machine.firmware_run(boot_lines("a"))
    efivars = tree(machine.efivars)
    machine.keeper(*args, expect=2)  # usage error

    assert not machine.merged_root.exists() and not machine.bootefi_root.exists() and not machine.memory_root.exists()
    assert tree(machine.efivars) == efivars
    machine.check()


def test_I1_the_commands(machine, tmp_path):
    run = machine.firmware_run(boot_lines("a"))
    machine.keeper()
    assert machine.log(run) == run.log()

    (tmp_path / "legacy").mkdir()
    machine.keeper("migrate", tmp_path / "legacy")  # nothing to import: still a valid command of the reference
    assert machine.log(run) == run.log()
    machine.check()


def test_I1_empty_old_root_is_a_usage_error(machine):
    """What a script does with an unset variable, from inside the archive: nothing is read, nothing is removed."""
    run = machine.firmware_run(boot_lines("a"))
    machine.add_record(record(run.reset + timedelta(seconds=2), 0x1A))
    machine.add_record(record(datetime(2026, 11, 19, 8, 0, 0), 0x1B))
    machine.keeper()
    before = tree(machine.merged_root)
    assert len(machine.record_copies()) == 2
    machine.keeper("migrate", "", expect=2, cwd=machine.merged_root)

    assert tree(machine.merged_root) == before
    machine.check()


def test_I2_defaults_boot_id_from_proc_and_time_in_utc(machine):
    """Without FWLK_BOOT_ID and FWLK_NOW, as on a real machine: the kernel's boot id without its dashes, and the
    current time in UTC whatever the time zone."""
    machine.firmware_run(boot_lines("a"))
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
    before = datetime.now(timezone.utc)
    machine.keeper(env={"FWLK_BOOT_ID": None, "FWLK_NOW": None, "TZ": "Pacific/Kiritimati"}, boot_id=boot)

    stamp, seen_boot = machine.bootefi_seen()[0][:2]
    assert seen_boot == boot
    assert [name.split("_")[1] for name in machine.folder_names()] == [boot]
    seen = datetime.strptime(stamp, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    assert abs(seen - before) < timedelta(minutes=2)  # Kiritimati is 14 hours ahead
    machine.check()


def test_I2_empty_variable_counts_as_unset(machine):
    """SPEC section 6. Tried on variables whose default is not a path of the system this test runs on."""
    run = machine.firmware_run(boot_lines("a"))
    res = machine.keeper(env={"FWLK_KEEP_RUNS": "", "FWLK_KEEP_BYTES": "", "FWLK_FAULT": ""})

    assert res.starting("error: ") == []
    assert machine.log(run) == run.log()
    machine.check()


@pytest.mark.skipif(bool(os.environ.get("FWLK_BIN")) or not os.access("/usr/bin/python3", os.X_OK),
                    reason="only for the Python reference launcher, on a system that has /usr/bin/python3")
def test_I1_reference_launcher_runs_by_its_own_first_line():
    """The suite runs the launcher under the interpreter of the tests; an installed copy runs by its first line.
    A usage error reads and writes nothing."""
    p = subprocess.run([str(DEFAULT_BIN), "status"], capture_output=True, timeout=60)
    assert p.returncode == 2 and p.stdout.startswith(b"usage: ") and p.stderr == b""
