"""Group E of TEST-SCENARIOS.md: safety of every delete."""
import lzma
from datetime import datetime, timedelta

import pytest

from builders import L, MemLog, boot_lines, esrt_text, fpdt_text, log_bytes, record, tree, unattached_rows
from conftest import not_root, read_only, writable

T0 = datetime(2026, 11, 20, 12, 0, 0)
S = timedelta(seconds=1)


@not_root
def test_E1_nothing_can_be_written(machine):
    """A read-only /var/log: no archive at all. Nothing is deleted, the run reports and exits 1."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    name = machine.add_record(record(T0 + 2 * S))
    var = machine.memory_root.parent
    var.mkdir()
    var.chmod(0o555)
    res = machine.keeper(expect=1)

    assert res.starting("error: ")
    assert res.starting("deleted error record ") == []
    assert machine.records_in_firmware() == [name]
    assert list(var.iterdir()) == []

    var.chmod(0o755)
    machine.keeper()  # the disk is back: everything is archived, then purged
    assert machine.log(run) == run.log([record(T0 + 2 * S)])
    assert machine.records_in_firmware() == []
    machine.check()


@not_root
def test_E1_merged_archive_cannot_be_written(machine):
    """The merged archive is read-only, staging is not: the sources stay where they are until the merge works."""
    first = machine.firmware_run(boot_lines("first"), reset=T0)
    machine.keeper()
    before = tree(machine.merged_root)
    run = machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    rec = record(run.reset + 2 * S)
    name = machine.add_record(rec)
    read_only(machine.merged_root)
    res = machine.keeper(expect=1)

    assert res.starting("error: ")
    assert machine.records_in_firmware() == [name]  # no archive copy, no deletion
    assert tree(machine.merged_root) == before
    staged = [p.name for p in machine.memory_root.iterdir()]
    assert staged == [f"{res.utc}_{run.boot_id}"]  # the memory log of this boot is still in staging
    assert (machine.memory_root / staged[0] / "advlog.bin").read_bytes() == run.mem.buffer()

    writable(machine.merged_root)
    machine.keeper()
    assert machine.folder_names() == [first.folder_name, run.folder_name]
    assert machine.log(run) == run.log([rec])
    assert machine.records_in_firmware() == []
    machine.check()  # H5: staging is empty again


def test_E2_crash_before_the_merge(machine):
    """A keeper run staged a boot's memory log and died. The next boot's run merges it, then removes it."""
    crashed = machine.firmware_run(boot_lines("crashed"), reset=T0)
    staging = machine.stage(crashed, datetime(2026, 11, 21, 4, 0, 0))
    run = machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    machine.keeper()

    assert not staging.exists()
    assert machine.folder_names() == [crashed.folder_name, run.folder_name]
    assert machine.log(crashed) == crashed.log()  # paired with its file log, which is still in its slot
    assert {r.source for r in machine.rows(crashed)} == {"both"}
    assert (machine.folder(crashed) / "fpdt-boot.txt").read_text() == fpdt_text(crashed.n)
    assert (machine.folder(crashed) / "esrt.txt").read_text() == esrt_text()
    assert machine.log(run) == run.log()
    assert (machine.folder(run) / "fpdt-boot.txt").read_text() == fpdt_text(run.n)
    machine.check()


def test_E2_crash_between_merge_and_purge(machine):
    """The merged log was written, then the keeper died: staging data and the error record are still there."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    machine.keeper()
    merged, seen = tree(machine.merged_root), machine.errrec_seen()
    staging = machine.stage(run, datetime(2026, 11, 21, 4, 0, 0))  # put back what the purge had removed
    name = machine.add_record(rec)
    res = machine.keeper()

    assert not staging.exists()
    assert machine.records_in_firmware() == []
    assert machine.errrec_seen() == seen + [(res.utc, run.boot_id, "deleted", name, rec.hash, "-")]
    after = tree(machine.merged_root)
    del merged["hwerrrec/seen.tsv"], after["hwerrrec/seen.tsv"]
    assert after == merged  # nothing merged twice
    machine.check()


def test_E3_merged_log_missing_a_line(machine):
    """FWLK_FAULT=drop-row removes the last row of a written log before it is checked: the check must fail."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 5 * S)  # its row is the last one of the log
    name = machine.add_record(rec)
    res = machine.keeper(expect=1, env={"FWLK_FAULT": "drop-row"})

    assert res.starting("error: ")
    staging = machine.memory_root / f"{res.utc}_{run.boot_id}"
    assert [p.name for p in machine.memory_root.iterdir()] == [staging.name]  # the staging data stays
    assert (staging / "advlog.bin").read_bytes() == run.mem.buffer()
    assert (staging / "fpdt-boot.txt").read_text() == fpdt_text(run.n)
    assert (staging / "esrt.txt").read_text() == esrt_text()
    assert machine.records_in_firmware() == [name]  # and so does the record
    assert res.starting("deleted error record ") == []
    assert (res.utc, run.boot_id, "kept", name, rec.hash, "not-verified") in machine.errrec_seen()

    machine.keeper()  # without the fault: the log is complete, then the purges happen
    assert machine.log(run) == run.log([rec])
    assert machine.rows(run)[-1].source == "errrec"
    assert machine.records_in_firmware() == []
    machine.check()


def test_E4_retention_by_number_of_runs(machine):
    machine.remove_uefilogs()
    runs, records = [], []
    for i in range(4):
        runs.append(machine.firmware_run(boot_lines(f"run{i}")))
        records.append(record(runs[-1].reset + 2 * S, 0x10 + i))  # one record in each run
        machine.add_record(records[-1])
        if i == 0:
            records.append(record(datetime(2026, 11, 19, 8, 0, 0), 0x99))  # and one in no run
            machine.add_record(records[-1])
        machine.keeper(env={"FWLK_KEEP_RUNS": "2"})
        assert machine.folder_names() == [run.folder_name for run in runs[-2:]]  # oldest removed, never the newest

    assert machine.record_copies() == {rec.hash for rec in records}  # the error-record archive is never pruned
    assert len([line for line in machine.errrec_seen() if line[2] == "archived"]) == 5
    assert len(machine.rows(runs[3])) == 10
    assert len(machine.unattached().splitlines()) == 2
    machine.check()


def test_E4_retention_removes_unreferenced_raw_file_logs(machine):
    first = machine.firmware_run(boot_lines("first"))
    machine.keeper()
    second = machine.firmware_run(boot_lines("second"))
    machine.keeper()
    machine.index = 0  # the rotation comes back to slot 1: the first boot's file is overwritten
    third = machine.firmware_run(boot_lines("third"))
    assert third.slot == first.slot == 1
    machine.keeper(env={"FWLK_KEEP_RUNS": "2"})

    assert machine.folder_names() == [second.folder_name, third.folder_name]
    assert machine.raw_logs() == {second.file.hash, third.file.hash}  # the first boot's raw copy went with its folder
    assert [line[3] for line in machine.bootefi_seen()].count(first.file.hash) == 2  # seen.tsv is never pruned
    machine.check()


def test_E4_retention_by_size_keeps_the_newest(machine):
    machine.remove_uefilogs()
    for i in range(3):
        run = machine.firmware_run(boot_lines(f"run{i}"))
        machine.keeper(env={"FWLK_KEEP_BYTES": "1"})
        assert machine.folder_names() == [run.folder_name]  # far above the limit, and still there
    machine.check()


def test_E2_crash_before_the_dump_got_its_name(machine):
    """A keeper run died after writing the memory dump under its temporary name: the dump is whole, the next
    boot's run merges it."""
    crashed = machine.firmware_run(boot_lines("crashed"), late=[L(6000, "after the hand-over")], reset=T0)
    staging = machine.stage(crashed, datetime(2026, 11, 21, 4, 0, 0))
    (staging / "advlog.bin").rename(staging / "advlog.bin.tmp")
    (staging / "fpdt-boot.txt").unlink()
    (staging / "esrt.txt").unlink()
    run = machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    machine.keeper()

    assert not staging.exists()
    assert machine.log(crashed) == crashed.log()
    assert machine.rows(crashed)[-1].message == "after the hand-over"
    assert machine.log(run) == run.log()
    machine.check()


@pytest.mark.parametrize("beside", [(), ("fpdt-boot.txt", "esrt.txt")])
def test_E2_staging_folder_with_nothing_readable_is_kept_and_reported(machine, beside):
    """A dump cut short under its temporary name is not merged, and the folder does not stay without a word,
    nor go with the dump because timings and versions are readable beside it."""
    crashed = machine.firmware_run(boot_lines("crashed"), reset=T0)
    staging = machine.stage(crashed, datetime(2026, 11, 21, 4, 0, 0))
    cut = (staging / "advlog.bin").read_bytes()[:-20]
    for name in {"advlog.bin", "fpdt-boot.txt", "esrt.txt"} - set(beside):
        (staging / name).unlink()
    (staging / "advlog.bin.tmp").write_bytes(cut)
    del machine.memory_seen[crashed.boot_id]
    run = machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    res = machine.keeper(expect=1)

    assert (staging / "advlog.bin.tmp").read_bytes() == cut
    assert [line for line in res.starting("error: memory: ") if str(staging) in line]
    assert machine.log(run) == run.log()  # the other sources are processed
    keys = {name.split("_")[1] for name in machine.folder_names()}
    assert keys == {f"fileonly-{crashed.file.hash}", run.boot_id} | ({crashed.boot_id} if beside else set())
    machine.check()


@pytest.mark.parametrize("legacy", ["advlog.bin.xz", "advlog.txt"])
def test_E2_staging_folder_with_only_a_legacy_file_is_kept_and_reported(machine, legacy):
    """Compressed dumps and decoded text are files of the legacy keeper: a keeper run reads neither in staging."""
    crashed = machine.firmware_run(boot_lines("crashed"), reset=T0)
    staging = machine.stage(crashed, datetime(2026, 11, 21, 4, 0, 0))
    dump = (staging / "advlog.bin").read_bytes()
    for name in ("advlog.bin", "fpdt-boot.txt", "esrt.txt"):
        (staging / name).unlink()
    (staging / legacy).write_bytes(lzma.compress(dump) if legacy.endswith(".xz") else b"# a legacy text\n")
    del machine.memory_seen[crashed.boot_id]
    run = machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    res = machine.keeper(expect=1)

    assert [p.name for p in staging.iterdir()] == [legacy]
    assert [line for line in res.starting("error: memory: ") if str(staging) in line]
    assert machine.log(run) == run.log()
    machine.check()


def test_E2_longer_dump_under_its_temporary_name_beside_advlog_bin(machine):
    """A first keeper run of a boot failed its merge, a second one died after writing a longer dump and before
    giving it its name: both dumps are offered, the longer one is the memory log (SPEC 4)."""
    machine.remove_uefilogs()
    lines = boot_lines("crashed")
    crashed = machine.firmware_run(lines, reset=T0)
    staging = machine.stage(crashed, datetime(2026, 11, 21, 4, 0, 0))
    longer = MemLog.from_lines(lines + [L(9000, "second dump only")], T0)
    (staging / "advlog.bin.tmp").write_bytes(longer.buffer())
    machine.memory_seen[crashed.boot_id] = longer
    machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    machine.keeper()

    assert not staging.exists()
    assert machine.rows(crashed)[-1].message == "second dump only"
    assert machine.sources(crashed) == f"memory\t{longer.hash}\t10\t-\t-\n"
    machine.check()


def test_E4_run_folder_left_under_its_removing_name_is_deleted(machine):
    """A run folder is renamed before it is removed (SPEC 4): a keeper run stopped in the middle leaves no half
    folder under a run folder's name, and the next run finishes the removal."""
    machine.remove_uefilogs()
    first = machine.firmware_run(boot_lines("first"))
    machine.keeper()
    folder = machine.folder(first)
    half = folder.with_name(folder.name + ".removing")
    folder.rename(half)
    (half / "firmware.log").unlink()  # stopped after the first file
    run = machine.firmware_run(boot_lines("now"))
    res = machine.keeper()

    assert not half.exists() and res.starting("error: ") == []
    assert machine.folder_names() == [run.folder_name]
    machine.check()


def test_E3_a_log_is_never_replaced_by_one_that_lacks_a_row_it_holds(machine):
    """Whatever sources.tsv says: the row of a record already deleted from the firmware exists nowhere else."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    machine.keeper()
    log, sources = machine.log(run), machine.folder(run) / "sources.tsv"
    listed = sources.read_bytes()
    sources.write_bytes(b"")  # damaged from outside: the folder no longer says what its log holds
    res = machine.keeper(expect=1)

    assert res.starting("error: merge: ") and machine.log(run) == log

    sources.write_bytes(listed)
    machine.keeper()
    assert machine.log(run) == run.log([rec])
    machine.check()


def test_E3_failed_check_keeps_the_log_that_was_there(machine):
    """The check of 3.5 is made on the new log before it replaces the old one. For a run whose staging data is
    long gone, the old log is the only copy of the memory lines."""
    machine.remove_uefilogs()
    older = machine.firmware_run(boot_lines("older"), reset=T0)
    machine.keeper()
    run = machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    rec = record(T0 + 3 * S)  # written during the older run: that run's log is to be rewritten
    name = machine.add_record(rec)
    res = machine.keeper(expect=1, env={"FWLK_FAULT": "drop-row"})

    assert res.starting("error: merge: ")
    assert machine.log(older) == older.log()  # byte for byte as it was
    assert machine.records_in_firmware() == [name]

    machine.keeper()  # without the fault
    assert machine.log(older) == older.log([rec])
    assert machine.log(run) == run.log()
    assert machine.records_in_firmware() == []
    machine.check()


def test_E3_run_folder_left_without_a_log_is_finished_by_the_next_run(machine):
    """A new log that fails its check is not written at all. The next run writes it from what the folder lists,
    even when the file log is no longer in UefiLogs by then."""
    hung = machine.firmware_run(boot_lines("hung"), linux=False)
    run = machine.firmware_run(boot_lines("now"))
    machine.keeper(expect=1, env={"FWLK_FAULT": "drop-row"})
    assert not (machine.folder(hung) / "firmware.log").exists() and not (machine.folder(run) / "firmware.log").exists()
    machine.remove_uefilogs()
    machine.keeper()

    assert machine.log(hung) == hung.log()
    assert machine.log(run) == run.log()
    machine.check()


def test_E3_failed_check_keeps_the_unattached_rows(machine):
    machine.remove_uefilogs()
    machine.firmware_run(boot_lines("a"), reset=T0)
    known = record(datetime(2026, 11, 19, 9, 0, 0), 0x2)
    machine.add_record(known)
    machine.keeper()
    assert machine.unattached() == log_bytes(unattached_rows([known]))
    earlier = record(datetime(2026, 11, 19, 8, 0, 0), 0x1)  # its row goes before: the row dropped is the known one
    name = machine.add_record(earlier)
    res = machine.keeper(expect=1, env={"FWLK_FAULT": "drop-row"})

    assert res.starting("error: errrec: ")
    assert machine.unattached() == log_bytes(unattached_rows([known]))
    assert machine.records_in_firmware() == [name]

    machine.keeper()
    assert machine.unattached() == log_bytes(unattached_rows([known, earlier]))
    assert machine.records_in_firmware() == []
    machine.check()


def test_E4_retention_follows_the_order_of_collection_not_the_firmware_clock(machine):
    """A boot whose hardware clock reads an old date (flat clock battery) is the newest run, not the oldest."""
    machine.remove_uefilogs()
    env = {"FWLK_KEEP_RUNS": "2"}
    machine.firmware_run(boot_lines("a"), reset=T0)
    machine.keeper(env=env)
    b = machine.firmware_run(boot_lines("b"), reset=T0 + timedelta(hours=1))
    machine.keeper(env=env)
    c = machine.firmware_run(boot_lines("c"), reset=datetime(2020, 1, 1, 0, 0, 0))
    rec = record(datetime(2020, 1, 1, 0, 0, 2))
    machine.add_record(rec)
    machine.keeper(env=env)

    assert machine.folder_names() == [c.folder_name, b.folder_name]
    assert machine.log(c) == c.log([rec])
    assert machine.records_in_firmware() == []

    d = machine.firmware_run(boot_lines("d"), reset=T0 + timedelta(hours=2))
    machine.keeper(env=env)
    assert machine.folder_names() == [c.folder_name, d.folder_name]  # b was collected before c
    runs = [line[2] for line in machine._tsv(machine.merged_root / "runs.tsv")]
    assert runs[1:] == [b.boot_id, c.boot_id, d.boot_id]  # runs.tsv is never pruned
    machine.check()


def test_E4_a_run_whose_file_log_is_still_in_its_slot_is_not_removed(machine):
    """Removed while its file is still in UefiLogs, a run would come back at the next keeper run as a run known
    by its file only. So retention waits until the firmware has overwritten the file."""
    env = {"FWLK_KEEP_RUNS": "3"}
    runs = []
    for i in range(11):
        runs.append(machine.firmware_run(boot_lines(f"run{i}")))
        machine.keeper(env=env)
        assert not [name for name in machine.folder_names() if "fileonly-" in name]
        assert machine.folder_names() == [run.folder_name for run in runs[-9:]]  # the nine slots

    again = machine.keeper(env=env)
    assert again.starting("archived ") == [] and again.starting("merged run ") == []
    assert machine.raw_logs() == {run.file.hash for run in runs[-9:]}
    machine.check()


def test_E5_the_three_roots_must_be_separate_folders(machine):
    """Staging folders are removed once merged: with the staging root in the archive, that would be run folders."""
    for tag in "ab":
        machine.firmware_run(boot_lines(tag))
        machine.keeper()
    machine.firmware_run(boot_lines("c"))
    before = tree(machine.merged_root.parent)
    for env in ({"FWLK_MEMORY_ROOT": str(machine.merged_root)},
                {"FWLK_MEMORY_ROOT": str(machine.merged_root / "staging")},
                {"FWLK_BOOTEFI_ROOT": str(machine.memory_root.parent)},
                # nor a folder of the firmware: the keeper never writes on the EFI partition or in efivars
                {"FWLK_BOOTEFI_ROOT": str(machine.uefilogs)},
                {"FWLK_MERGED_ROOT": str(machine.uefilogs / "archive")},
                {"FWLK_MEMORY_ROOT": str(machine.esp)},
                {"FWLK_MERGED_ROOT": str(machine.efivars)}):
        res = machine.keeper(expect=1, env=env)
        assert len(res.lines) == 1 and res.starting("error: keeper: ")
        assert tree(machine.merged_root.parent) == before
    machine.keeper()
    assert len(machine.folder_names()) == 3
    machine.check()


def test_E3_a_memory_row_without_its_uptime_stops_only_its_run(machine):
    """A log damaged from outside: one memory row has lost its uptime. That log cannot be compared with a dump, so
    its run reports an error and keeps its log, and the keeper run goes on with the other sources."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    machine.keeper()
    path = machine.folder(run) / "firmware.log"
    log = path.read_bytes()
    rows = [line.split("\t") for line in log.decode().split("\n")]
    for f in rows[1:-1]:
        if f[5] == "2":
            f[1] = "-"
    damaged = "\n".join("\t".join(f) for f in rows).encode()
    path.write_bytes(damaged)
    run.mem = MemLog.from_lines(boot_lines("a") + [L(9000, "runtime line")], run.reset)  # the firmware went on
    run.mem.write(machine.devmem, machine.efivars)
    name = machine.add_record(record(datetime(2026, 11, 19, 8, 0, 0)))
    res = machine.keeper(expect=1)

    assert res.starting("error: merge: ") and res.starting("error: keeper: ") == []
    assert path.read_bytes() == damaged
    assert res.starting("deleted error record ") == [f"deleted error record {name}"]  # the other sources: done

    path.write_bytes(log)
    machine.keeper()
    assert machine.log(run) == run.log()
    machine.check()
