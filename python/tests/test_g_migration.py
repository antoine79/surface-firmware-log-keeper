"""Group G of TEST-SCENARIOS.md: migration from the legacy keeper's layout (SPEC section 8)."""
import shutil
from datetime import datetime, timedelta

import pytest

from builders import (LEGACY_INDEX, FileLog, L, MemLog, boot_lines, esrt_text, fpdt_text, legacy_folder, legacy_records,
                      legacy_text, log_bytes, merge, record, sha16, tree, unattached_rows)

T0 = datetime(2026, 11, 20, 12, 0, 0)
S = timedelta(seconds=1)
HOUR = timedelta(hours=1)


class Legacy:
    """A legacy archive of three Linux boots and two error records, and the EFI partition as it is at migration time:
    the file logs of the last two boots and of one run that never reached Linux."""

    def __init__(self, machine, root, **folder_options):
        self.machine, self.root = machine, root
        self.boots = [machine.new_boot_id() for _ in range(3)]
        # 11 lines per boot; an empty line and an indented one, as the real logs have
        self.lines = [boot_lines(f"legacy{i}")[:-1] + [L(2000, "", 0x80000004), L(2100, "  indented"), L(5000, f"legacy{i} boot loader")]
                      for i in range(3)]
        self.mems = [MemLog.from_lines(self.lines[i], T0 + i * HOUR) for i in range(3)]
        self.inside = record(T0 + HOUR + 2 * S, 0x1A)  # during the second boot
        self.outside = record(datetime(2026, 11, 19, 8, 0, 0), 0x1B)  # in no run
        self.folders = [
            legacy_folder(root, f"2026-11-20T0{3 + i}0000Z", self.boots[i], self.mems[i], i + 1,
                          records=[self.inside, self.outside], **folder_options)
            for i in range(3)]
        legacy_records(root, [self.inside, self.outside])
        self.files = [None] + [FileLog(self.lines[i], T0 + i * HOUR + 5 * S) for i in (1, 2)]
        self.stranger = FileLog(boot_lines("stranger"), T0 + 3 * HOUR + 5 * S)
        for file in self.files[1:] + [self.stranger]:
            machine.write_file(file)
        for boot, mem in zip(self.boots, self.mems):
            machine.memory_seen[boot] = mem
        self.now_boot = machine.new_boot_id()  # the Linux boot in which the migration runs

    def migrate(self, root=None, **kw):
        return self.machine.keeper("migrate", root or self.root, boot_id=self.now_boot, **kw)

    def expected_logs(self) -> dict[str, bytes]:
        return {self.boots[0]: log_bytes(merge(self.mems[0])),
                self.boots[1]: log_bytes(merge(self.mems[1], self.files[1], [self.inside])),
                self.boots[2]: log_bytes(merge(self.mems[2], self.files[2])),
                f"fileonly-{self.stranger.hash}": log_bytes(merge(None, self.stranger))}

    def assert_migrated(self, memory_hashes=None):
        machine = self.machine
        expected = self.expected_logs()
        assert sorted(name.split("_")[1] for name in machine.folder_names()) == sorted(expected)
        for key, log in expected.items():
            assert machine.log(key) == log
        for i, boot in enumerate(self.boots):
            folder = machine.folder(boot)
            assert folder.name == f"{(T0 + i * HOUR).strftime('%Y-%m-%dT%H%M%S')}_{boot}"
            assert (folder / "fpdt-boot.txt").read_text() == fpdt_text(i + 1)
            assert (folder / "esrt.txt").read_text() == esrt_text()
            memory_hash = (memory_hashes or [m.hash for m in self.mems])[i]
            assert machine.sources(boot).startswith(f"memory\t{memory_hash}\t11\t-\t-\n")
        assert machine.unattached() == log_bytes(unattached_rows([self.outside]))
        # the error-record archive: every record under the hash of its whole content, and the legacy history
        assert machine.record_copies() == {self.inside.hash, self.outside.hash}
        assert (machine.merged_root / "hwerrrec" / f"{self.inside.hash}.bin").read_bytes() == self.inside.raw
        assert (machine.merged_root / "hwerrrec" / "legacy-index.txt").read_text() == LEGACY_INDEX
        assert sorted((line[2], line[4]) for line in machine.errrec_seen()) == sorted(
            [("archived", self.inside.hash), ("archived", self.outside.hash)])
        assert not any(folder.exists() for folder in self.folders)  # removed once their merged folder is verified


def test_G1_legacy_folders_become_merged_folders(machine, tmp_path):
    legacy = Legacy(machine, tmp_path / "legacy")
    res = legacy.migrate()

    legacy.assert_migrated()
    assert res.merged(legacy.boots[0])[1:] == (11, 0, 0)
    assert res.merged(legacy.boots[1])[1:] == (11, 11, 1)
    assert res.merged(f"fileonly-{legacy.stranger.hash}")[1:] == (0, 9, 0)
    assert machine.raw_logs() == {legacy.files[1].hash, legacy.files[2].hash, legacy.stranger.hash}
    assert machine.sources(legacy.boots[1]) == (f"memory\t{legacy.mems[1].hash}\t11\t-\t-\n"
                                                f"bootefi\t{legacy.files[1].hash}\t11\tUEFI_Log1.txt\tmerged\n"
                                                f"errrec\t{legacy.inside.hash}\t1\t-\t-\n")
    machine.check()


def test_G2_legacy_folder_removed_only_after_verification(machine, tmp_path):
    legacy = Legacy(machine, tmp_path / "legacy")
    before = tree(legacy.root)
    res = legacy.migrate(expect=1, env={"FWLK_FAULT": "drop-row"})  # every written log loses its last row

    assert res.starting("error: ")
    assert tree(legacy.root) == before  # the forced verification failure keeps every legacy folder

    legacy.migrate()
    legacy.assert_migrated()
    machine.check()


@pytest.mark.parametrize("keep, xz", [(("advlog.txt",), False), (("advlog.bin", "advlog.txt"), True),
                                      (("advlog.txt",), True)])
def test_G3_decoded_text_only_or_compressed(machine, tmp_path, keep, xz):
    legacy = Legacy(machine, tmp_path / "legacy", keep=keep, xz=xz)
    names = sorted(p.name for p in legacy.folders[0].iterdir() if p.name.startswith("advlog"))
    assert names == [name + (".xz" if xz else "") for name in keep]
    legacy.migrate()

    # the memory log's hash is the hash of the file it was read from, uncompressed
    used = [m.buffer() if "advlog.bin" in keep else legacy_text(m).encode() for m in legacy.mems]
    legacy.assert_migrated(memory_hashes=[sha16(data) for data in used])
    machine.check()


def test_G4_migration_run_twice(machine, tmp_path):
    legacy = Legacy(machine, tmp_path / "legacy")
    shutil.copytree(legacy.root, tmp_path / "copy")
    legacy.migrate()
    merged, raw = tree(machine.merged_root), machine.raw_logs()

    legacy.migrate()  # on what is left of the legacy root
    assert tree(machine.merged_root) == merged
    legacy.migrate(tmp_path / "copy")  # and on an untouched copy of it
    assert tree(machine.merged_root) == merged and machine.raw_logs() == raw
    assert not any((tmp_path / "copy" / folder.name).exists() for folder in legacy.folders)
    legacy.assert_migrated()
    machine.check()


def test_G5_migrate_refuses_a_folder_of_the_archive(machine):
    """The legacy root and the merged root have the same default path: `migrate` on the archive itself, on a
    folder inside it or on a folder that holds it must not read or remove anything."""
    for tag in "abc":
        machine.firmware_run(boot_lines(tag))
        machine.keeper()
    var = machine.merged_root.parent
    before = tree(var)
    assert len(machine.folder_names()) == 3
    for old in (machine.merged_root, machine.merged_root / "hwerrrec", var, machine.bootefi_root, machine.memory_root,
                machine.uefilogs, machine.esp):
        old.mkdir(exist_ok=True)
        res = machine.keeper("migrate", old, expect=1)
        assert len(res.lines) == 1 and res.starting("error: migrate: ")
        assert tree(var) == before
    machine.check()


def _keeper_then_legacy_folder(machine, tmp_path, legacy_lines):
    """The new keeper has run in a boot (memory log with lines after the hand-over); the legacy keeper has a
    folder for the same boot, with the dump it took."""
    lines = boot_lines("a")
    run = machine.firmware_run(lines, late=[L(6000 + i, f"runtime line {i}") for i in range(4)], reset=T0)
    machine.keeper()
    assert machine.log(run) == run.log() and len(machine.rows(run)) == 13
    folder = legacy_folder(tmp_path / "legacy", "2026-11-20T030000Z", run.boot_id,
                           MemLog.from_lines(legacy_lines(lines), T0), run.n)
    return run, folder


def test_G6_migrate_a_shorter_dump_of_a_boot_already_archived(machine, tmp_path):
    """The legacy dump was taken earlier in the boot: all its lines are in the archived log, which stays."""
    run, folder = _keeper_then_legacy_folder(machine, tmp_path, lambda lines: lines)
    before = tree(machine.merged_root)
    machine.keeper("migrate", tmp_path / "legacy")

    assert tree(machine.merged_root) == before
    assert not folder.exists()  # nothing in it that the checked log does not have
    machine.check()


def test_G6_migrate_another_dump_of_a_boot_already_archived(machine, tmp_path):
    """The legacy dump has a line the archived log does not have: neither replaces the other, the folder stays."""
    run, folder = _keeper_then_legacy_folder(machine, tmp_path, lambda lines: lines[:3] + [L(900, "legacy only")])
    before, legacy = tree(machine.merged_root), tree(tmp_path / "legacy")
    res = machine.keeper("migrate", tmp_path / "legacy", expect=1)

    assert tree(machine.merged_root) == before and tree(tmp_path / "legacy") == legacy
    assert [line for line in res.starting("error: memory: ") if str(folder) in line]
    machine.check()


def test_G7_same_archive_whether_the_keeper_or_migrate_runs_first(machines, tmp_path):
    """A record still in the firmware, written during the first legacy boot. If the keeper runs before `migrate`
    the record first goes to unattached/, then moves to its run when the run is imported."""
    logs = []
    for keeper_first in (False, True):
        machine = machines()
        legacy = Legacy(machine, tmp_path / f"legacy{len(logs)}")
        old = record(T0 + 2 * S, 0x2A)
        machine.add_record(old)
        if keeper_first:
            machine.keeper(boot_id=legacy.now_boot)
            assert machine.unattached() == log_bytes(unattached_rows([old]))
        legacy.migrate()
        if not keeper_first:
            machine.keeper(boot_id=legacy.now_boot)
        assert machine.log(legacy.boots[0]) == log_bytes(merge(legacy.mems[0], None, [old]))
        assert machine.unattached() == log_bytes(unattached_rows([legacy.outside]))
        logs.append({path: data for path, data in tree(machine.merged_root).items() if path.endswith("firmware.log")})
        machine.check()
    assert logs[0] == logs[1]


def test_G1_record_copy_found_only_in_a_legacy_boot_folder(machine, tmp_path):
    """The legacy keeper wrote a record into the boot folder before its archive: a copy can be in the folder
    alone. It is archived and gets its row before the folder goes (SPEC 8)."""
    machine.remove_uefilogs()
    boot, mem, rec = machine.new_boot_id(), MemLog.from_lines(boot_lines("legacy"), T0), record(T0 + 2 * S)
    folder = legacy_folder(tmp_path / "legacy", "2026-11-20T030000Z", boot, mem, 1, records=[rec])
    legacy_records(tmp_path / "legacy", [])
    machine.memory_seen[boot] = mem
    machine.keeper("migrate", tmp_path / "legacy", boot_id=machine.new_boot_id())

    assert not folder.exists()
    assert (machine.merged_root / "hwerrrec" / f"{rec.hash}.bin").read_bytes() == rec.raw
    assert machine.log(boot) == log_bytes(merge(mem, None, [rec]))
    machine.check()


def test_G1_retention_after_migrate_removes_the_oldest_legacy_folder_first(machine, tmp_path):
    """runs.tsv notes the migrated runs in the order of the legacy folder names, which begin with the time they
    were collected at, not in the order of the boot ids, which are random (SPEC 2)."""
    machine.remove_uefilogs()
    boots = ["c" * 32, "b" * 32, "a" * 32]  # sorted against their dates
    for i, boot in enumerate(boots):
        mem = MemLog.from_lines(boot_lines(f"legacy{i}"), T0 + i * HOUR)
        legacy_folder(tmp_path / "legacy", f"2026-11-20T0{3 + i}0000Z", boot, mem, i + 1)
        machine.memory_seen[boot] = mem
    machine.keeper("migrate", tmp_path / "legacy", boot_id=machine.new_boot_id())
    assert [line[2] for line in machine._tsv(machine.merged_root / "runs.tsv")] == boots

    run = machine.firmware_run(boot_lines("now"), reset=T0 + 5 * HOUR)
    machine.keeper(env={"FWLK_KEEP_RUNS": "2"})
    assert {name.split("_")[1] for name in machine.folder_names()} == {boots[2], run.boot_id}  # the two newest
    machine.check()
