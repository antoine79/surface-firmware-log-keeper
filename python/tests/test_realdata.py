"""Scenario J1: the keeper on a copy of a real machine's data. Local only, nothing of it is in the repository.

Runs only when FWLK_REALDATA points to a snapshot holding var-log-firmware/ (a legacy keeper archive), UefiLogs/
(the folder of the EFI partition) and efivars/ (the firmware variables). The snapshot is copied first and never
written. FWLK_REALDATA_NOLINUX, if set, is the number of file-only runs expected (found by hand).
"""
import lzma
import os
import re
import shutil
import struct
from pathlib import Path

import pytest

from builders import LOCATOR_VAR, PAD_BYTES, SUPPORT_VAR, norm, parse_content, parse_log, sha16, tree
from conftest import RECORD_VAR_RE, digest

SNAPSHOT = os.environ.get("FWLK_REALDATA")
pytestmark = pytest.mark.skipif(not SNAPSHOT, reason="FWLK_REALDATA is not set")
LEGACY_LINE = re.compile(r" *(\d+)\.(\d{3}) \S+ L[0-9a-f]{8} ?(.*)")


def read_maybe_xz(path: Path) -> bytes:
    packed = path.with_name(path.name + ".xz")
    return lzma.decompress(packed.read_bytes()) if packed.exists() else path.read_bytes()


def legacy_lines(folder: Path) -> list[tuple[int, str]]:
    """(uptime in ms as the legacy keeper rounded it, text) of every line of a legacy advlog.txt."""
    out = []
    for line in read_maybe_xz(folder / "advlog.txt").decode("utf-8").split("\n"):
        if line and not line.startswith("#"):
            m = LEGACY_LINE.fullmatch(line)
            assert m, f"legacy line not understood: {line!r}"
            out.append((int(m[1]) * 1000 + int(m[2]), norm(m[3])))
    return out


def same_run(file_lines, memory_lines) -> bool:
    """Is this file log the beginning of that legacy memory log? Every line must be there within 2 ms: the legacy
    text has rounded times taken from a line's first entry, the file has truncated times from its last entry."""
    times = {}
    for ms, text in memory_lines:
        times.setdefault(text, []).append(ms)
    hits = sum(any(abs(x.ms - ms) <= 2 for ms in times.get(norm(x.text), ())) for x in file_lines)
    return hits >= 0.99 * len(file_lines)


def test_J1_real_snapshot(machine, tmp_path):
    snapshot = Path(SNAPSHOT)
    untouched = digest(snapshot)
    legacy = tmp_path / "legacy"
    shutil.copytree(snapshot / "var-log-firmware", legacy)
    for target, source in ((machine.efivars, "efivars"), (machine.uefilogs, "UefiLogs")):
        shutil.rmtree(target)
        shutil.copytree(snapshot / source, target)
    for path in [legacy, machine.efivars, machine.uefilogs]:
        for p in [path, *path.rglob("*")]:
            p.chmod(0o755 if p.is_dir() else 0o644)

    # what the snapshot holds, read before the keeper touches the copy
    boot_folders = sorted(p for p in legacy.iterdir() if p.is_dir() and p.name != "hwerrrec")
    memory = {p.name.split("_")[1]: legacy_lines(p) for p in boot_folders}
    assert len(memory) == len(boot_folders)
    files = {}
    for p in sorted(machine.uefilogs.glob("UEFI_Log[1-9].txt")):
        content = p.read_bytes().rstrip(PAD_BYTES)
        if content:
            files[sha16(content)] = parse_content(content)
    owner = {h: [boot for boot, lines in memory.items() if same_run(rows, lines)] for h, rows in files.items()}
    assert all(len(boots) <= 1 for boots in owner.values())
    variables = {p.name: p.read_bytes() for p in machine.efivars.iterdir()}
    records = {name: data for name, data in variables.items() if RECORD_VAR_RE.fullmatch(name)}
    old_copies = [p.read_bytes() for p in (legacy / "hwerrrec").glob("*.bin")]

    # the keeper runs in the boot of the newest legacy folder: its memory log is put back in a fake /dev/mem
    boot_id = boot_folders[-1].name.split("_")[1]
    address = struct.unpack_from("<Q", variables[LOCATOR_VAR], 4)[0]
    with open(machine.devmem, "wb") as f:
        f.seek(address)
        f.write(read_maybe_xz(boot_folders[-1] / "advlog.bin"))

    machine.keeper("migrate", legacy, boot_id=boot_id, reference=True)  # a command of the Python reference only
    machine.keeper(boot_id=boot_id)
    merged = tree(machine.merged_root)
    machine.keeper(boot_id=boot_id)
    after = tree(machine.merged_root)
    assert after == merged  # the second run finds nothing new

    # every legacy memory-log line is in its boot's merged log, exactly once, in order
    for boot, lines in memory.items():
        rows = sorted((r for r in machine.rows(boot) if r.mem_line != "-"), key=lambda r: int(r.mem_line))
        assert [r.message for r in rows] == [text for _, text in lines], f"memory lines of boot {boot}"
    assert not any(p.exists() for p in boot_folders)  # each legacy folder went once its merged folder was verified

    # every file log is in one run: its boot's when the memory log has its lines, else a run that never reached Linux
    listed = {}
    for name in machine.folder_names():
        for line in (machine.merged_root / name / "sources.tsv").read_text().splitlines():
            kind, h = line.split("\t")[:2]
            if kind == "bootefi":
                listed[h] = name.split("_")[1]
    assert listed == {h: boots[0] if boots else f"fileonly-{h}" for h, boots in owner.items()}
    file_only = [key for key in listed.values() if key.startswith("fileonly-")]
    for key in file_only:
        assert {r.source for r in machine.rows(key)} <= {"bootefi", "errrec"}
    if os.environ.get("FWLK_REALDATA_NOLINUX"):
        assert len(file_only) == int(os.environ["FWLK_REALDATA_NOLINUX"])

    # every error record: archived, one row (in a run's log or in unattached/), deleted from the copy only
    logs = [parse_log(data) for path, data in after.items() if path.endswith("firmware.log")]
    record_rows = [r.message for rows in logs for r in rows if r.source == "errrec"]
    for data in list(records.values()) + old_copies:
        h = sha16(data)
        assert after[f"hwerrrec/{h}.bin"] == data
        assert sum(f"hash={h}" in message for message in record_rows) == 1, f"rows of record {h}"
    assert after["hwerrrec/legacy-index.txt"] == (snapshot / "var-log-firmware" / "hwerrrec" / "index.txt").read_bytes()
    left = {p.name: p.read_bytes() for p in machine.efivars.iterdir()}
    assert left == {name: data for name, data in variables.items() if name not in records}
    assert SUPPORT_VAR in left and LOCATOR_VAR in left
    assert digest(snapshot) == untouched

    machine.check()  # H1 to H6
