"""Group I of TEST-SCENARIOS.md: the keeper has one command, a run."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from builders import boot_lines, tree


@pytest.mark.parametrize("args", [("decode-cper", "x.bin"), ("decode-advlog", "advlog.bin"), ("status",), ("run",),
                                  ("report",), ("migrate", "old"), ("",)])
def test_I1_no_other_command(machine, args):
    machine.firmware_run(boot_lines("a"))
    efivars = tree(machine.efivars)
    machine.keeper(*args, expect=2)  # usage error

    assert not machine.merged_root.exists() and not machine.bootefi_root.exists() and not machine.memory_root.exists()
    assert tree(machine.efivars) == efivars
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
    res = machine.keeper(env={"FWLK_NOW": "", "FWLK_FAULT": ""})

    assert res.starting("error: ") == []
    assert machine.log(run) == run.log()
    machine.check()
