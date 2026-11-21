"""Scenario J1: the keeper on a copy of an archive the installed keeper wrote. Local only, nothing of it is in the
repository.

Runs only when FWLK_REALDATA points to a snapshot holding firmware/ and firmware_bootefilogs/ (the archive),
UefiLogs/ (the folder of the EFI partition) and efivars/ (records there are already archived). The snapshot is
copied first and never written.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from builders import tree
from conftest import RECORD_VAR_RE, keeper_bin

SNAPSHOT = os.environ.get("FWLK_REALDATA")
pytestmark = pytest.mark.skipif(not SNAPSHOT, reason="FWLK_REALDATA is not set")


def run_keeper(root: Path, now: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FWLK_")}
    env.update({"FWLK_MEMORY_ROOT": str(root / "staging"), "FWLK_BOOTEFI_ROOT": str(root / "firmware_bootefilogs"),
                "FWLK_MERGED_ROOT": str(root / "firmware"), "FWLK_EFIVARS": str(root / "efivars"),
                "FWLK_UEFILOGS": str(root / "UefiLogs"), "FWLK_DEVMEM": str(root / "nowhere"),
                "FWLK_SYSFS_FPDT": str(root / "nowhere"), "FWLK_SYSFS_ESRT": str(root / "nowhere"),
                "FWLK_BOOT_ID": "0123456789abcdef0123456789abcdef", "FWLK_NOW": now})
    return subprocess.run(keeper_bin(), env=env, capture_output=True, timeout=600)


def test_J1_real_archive(tmp_path):
    root = tmp_path / "live"
    shutil.copytree(SNAPSHOT, root, symlinks=True)
    (root / "staging").mkdir()
    variables = {p.name: p.read_bytes() for p in (root / "efivars").iterdir()}
    records = {name for name in variables if RECORD_VAR_RE.fullmatch(name)}
    esp = tree(root / "UefiLogs")
    uefilogs = sorted((root / "UefiLogs").rglob("*"))
    for p in uefilogs:  # an access time older than the change time: any plain read would update it
        os.utime(p, ns=(p.stat().st_mtime_ns - 3600 * 10**9, p.stat().st_mtime_ns))
    accessed = [p.stat().st_atime_ns for p in uefilogs]
    logs = {k: v for k, v in tree(root / "firmware").items() if k.endswith("firmware.log")}

    first = run_keeper(root, "2026-11-22T05:00:00Z")
    assert (first.returncode, first.stderr) == (0, b""), first.stdout.decode()
    after_first = {**tree(root / "firmware"), **{f"bootefi/{k}": v for k, v in tree(root / "firmware_bootefilogs").items()}}
    second = run_keeper(root, "2026-11-22T05:01:00Z")
    assert (second.returncode, second.stderr) == (0, b""), second.stdout.decode()
    after = {**tree(root / "firmware"), **{f"bootefi/{k}": v for k, v in tree(root / "firmware_bootefilogs").items()}}

    assert {k: v for k, v in after.items() if k.endswith("firmware.log")} == logs  # not one byte changed
    left = {p.name: p.read_bytes() for p in (root / "efivars").iterdir()}
    assert left == {name: data for name, data in variables.items() if name not in records}  # the records, and only they
    assert len(left) == 2
    assert [p.stat().st_atime_ns for p in uefilogs] == accessed  # before reading them here
    assert tree(root / "UefiLogs") == esp
    assert list((root / "staging").iterdir()) == []
    # the second run: nothing new but lines of the seen.tsv files
    assert after.keys() == after_first.keys()
    for name, data in after.items():
        if name.endswith("seen.tsv"):
            assert data.startswith(after_first[name]), name
        else:
            assert data == after_first[name], name
