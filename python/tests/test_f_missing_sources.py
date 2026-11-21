"""Group F of TEST-SCENARIOS.md: missing sources and errors."""
import shutil
import uuid
from datetime import datetime, timedelta

import pytest

from builders import COLUMNS, LOCATOR_VAR, L, boot_lines, esrt_text, fpdt_text, label, log_bytes, merge, record
from conftest import not_root

T0 = datetime(2026, 11, 20, 12, 0, 0)
S = timedelta(seconds=1)


def _lose_memory(machine, how):
    if how == "no locator variable":
        (machine.efivars / LOCATOR_VAR).unlink()
    elif how == "no /dev/mem":
        machine.devmem.unlink()
    else:
        machine.devmem.chmod(0)


@pytest.mark.parametrize("how", ["no locator variable", "no /dev/mem", pytest.param("unreadable /dev/mem", marks=not_root)])
def test_F1_no_memory_log(machine, how):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    _lose_memory(machine, how)
    res = machine.keeper()

    assert len(res.starting("skipped memory: ")) == 1  # one message, and the other sources are processed
    assert res.starting("error: ") == []
    # the file log: without the memory log nothing says it is this boot's, so it is a run of its own
    file_only = f"2026-11-20T120000_fileonly-{run.file.hash}"
    assert (machine.merged_root / file_only / "firmware.log").read_bytes() == log_bytes(merge(None, run.file, [rec]))
    assert machine.records_in_firmware() == []
    # timings and versions: in this boot's folder, named after the keeper run since no firmware clock is known
    linux = f"{label(res.now)}_{run.boot_id}"
    assert machine.folder_names() == sorted([file_only, linux])
    assert (machine.merged_root / linux / "firmware.log").read_text() == COLUMNS
    assert (machine.merged_root / linux / "sources.tsv").read_text() == ""
    assert (machine.merged_root / linux / "fpdt-boot.txt").read_text() == fpdt_text(run.n)
    assert (machine.merged_root / linux / "esrt.txt").read_text() == esrt_text()
    machine.check()


def test_F1_memory_log_readable_at_a_later_run(machine):
    """SPEC 2.1: a fileonly- folder that turns out to belong to a run that reached Linux is merged into it."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    locator = (machine.efivars / LOCATOR_VAR).read_bytes()
    _lose_memory(machine, "no locator variable")
    machine.keeper()
    assert len(machine.folder_names()) == 2

    (machine.efivars / LOCATOR_VAR).write_bytes(locator)
    machine.keeper()
    assert machine.folder_names() == [run.folder_name]  # one folder, renamed now that its firmware-clock time is known
    assert machine.log(run) == run.log([rec])
    assert machine.sources(run) == (f"memory\t{run.mem.hash}\t9\t-\t-\n"
                                    f"bootefi\t{run.file.hash}\t9\tUEFI_Log1.txt\tmerged\n"
                                    f"errrec\t{rec.hash}\t1\t-\t-\n")
    machine.check()


def test_F2_no_uefilogs_folder(machine):
    machine.remove_uefilogs()
    shutil.rmtree(machine.esp)  # no EFI partition mounted at all
    run = machine.firmware_run(boot_lines("a"))
    rec = record(run.reset + 2 * S)
    machine.add_record(rec)
    res = machine.keeper()

    assert len(res.starting("skipped bootefi: ")) == 1
    assert res.starting("error: ") == []
    assert machine.log(run) == run.log([rec])
    assert not machine.esp.exists() and machine.raw_logs() == set()
    assert machine.records_in_firmware() == []
    machine.check()


def test_F3_no_records_timings_or_versions(machine):
    run = machine.firmware_run(boot_lines("a"))
    shutil.rmtree(machine.fpdt)
    shutil.rmtree(machine.esrt)
    res = machine.keeper()

    skipped = [line.split(":")[0] for line in res.starting("skipped ")]
    assert set(skipped) <= {"skipped errrec", "skipped fpdt", "skipped esrt"}  # quietly, or one message each
    assert len(skipped) == len(set(skipped))
    assert res.starting("error: ") == []
    assert sorted(p.name for p in machine.folder(run).iterdir()) == ["firmware.log", "sources.tsv"]
    assert machine.log(run) == run.log()
    machine.check()


def test_F4_one_step_fails_the_others_still_run(machine):
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    machine.corrupt_memory()  # the locator variable points at something that is not a log buffer
    res = machine.keeper(expect=1)

    assert len(res.starting("error: memory: ")) == 1
    file_only = f"fileonly-{run.file.hash}"
    assert machine.log(file_only) == log_bytes(merge(None, run.file, [rec]))  # file logs and error records: done
    assert machine.records_in_firmware() == []
    assert (machine.folder(run) / "fpdt-boot.txt").read_text() == fpdt_text(run.n)  # timings and versions: done
    assert (machine.folder(run) / "esrt.txt").read_text() == esrt_text()
    machine.check()


def test_F5_two_hour_firmware_stage_with_repeated_lines(machine):
    """Two hours in the firmware, the same line thousands of times: merged like any other log, without a word."""
    lines = [L(374, "SEC start")]
    for i in range(1500):  # every 4.8 s, three times in the same millisecond
        lines += [L(1000 + 4800 * i, "XhcControlTransfer: error - Time out, transfer - 0")] * 3
    lines.append(L(7_200_000, "Boot loader"))
    run = machine.firmware_run(lines, reset=T0)
    res = machine.keeper()

    assert b"\r\n02:00:00.000 : Boot loader\r\n" in run.file.content
    assert machine.log(run) == run.log()
    rows = machine.rows(run)
    assert len(rows) == 4502 and {r.source for r in rows} == {"both"}
    assert rows[-1].text() == "2026-11-20T14:00:00.000\t7200.000\t0\tL80000000\tboth\t4502\t4502\tBoot loader"
    assert res.merged(run.boot_id) == (run.folder_name, 4502, 4502, 0)
    others = [line for line in res.lines if not line.startswith(("skipped ", "merged run "))]
    assert others == [f"archived file log {run.file.hash} from UEFI_Log1.txt"]  # and not a word about the content
    machine.check()


def _file_only_then_readable(machine):
    """A boot whose first keeper run cannot read the memory log: the file log and a record go to a fileonly- folder."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    locator = (machine.efivars / LOCATOR_VAR).read_bytes()
    _lose_memory(machine, "no locator variable")
    machine.keeper()
    (machine.efivars / LOCATOR_VAR).write_bytes(locator)  # readable from now on
    return run, rec, machine.folder(f"fileonly-{run.file.hash}")


def test_F1_absorbed_folder_kept_while_its_record_has_no_row_elsewhere(machine):
    run, rec, file_only = _file_only_then_readable(machine)
    copy = machine.merged_root / "hwerrrec" / f"{rec.hash}.bin"
    copy.write_bytes(b"damaged")  # the record's row cannot be made again
    res = machine.keeper(expect=1)

    assert rec.message in (file_only / "firmware.log").read_text()  # the last readable trace of the record
    assert [line for line in res.starting("error: purge: ") if file_only.name in line]

    # a later boot, with the archive above its limit: retention does not take the folder the purge keeps
    later = machine.firmware_run(boot_lines("b"), reset=T0 + timedelta(hours=1))
    res = machine.keeper(expect=1, env={"FWLK_KEEP_RUNS": "2"})
    assert rec.message in (file_only / "firmware.log").read_text()
    assert [line for line in res.starting("error: purge: ") if file_only.name in line]

    copy.write_bytes(rec.raw)  # repaired: the next run finishes
    machine.keeper()
    assert machine.folder_names() == [run.folder_name, later.folder_name]
    assert machine.log(run) == run.log([rec])
    machine.check()


def test_F1_absorbed_folder_whose_sources_cannot_be_read_is_kept(machine):
    """What the folder holds is not known: it stays, with an error, and the record's row with it."""
    run, rec, file_only = _file_only_then_readable(machine)
    sources = file_only / "sources.tsv"
    listed = sources.read_text()
    sources.write_text(listed[:listed.index("errrec") + 20])  # cut inside the line of the record
    res = machine.keeper(expect=1)

    assert rec.message in (file_only / "firmware.log").read_text()
    assert [line for line in res.starting("error: purge: ") if file_only.name in line]

    sources.write_text(listed)
    machine.keeper()
    assert machine.folder_names() == [run.folder_name]
    assert machine.log(run) == run.log([rec])
    machine.check()


def test_F1_fileonly_folder_already_merged_goes_even_when_its_file_left_uefilogs(machine):
    """The boot's folder lists the file log but the fileonly- folder is still there (the boot's log failed its
    check); then the file is no longer in UefiLogs. The next run still removes the duplicate (SPEC 2.1)."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    locator = (machine.efivars / LOCATOR_VAR).read_bytes()
    _lose_memory(machine, "no locator variable")
    machine.keeper()
    (machine.efivars / LOCATOR_VAR).write_bytes(locator)
    machine.keeper(expect=1, env={"FWLK_FAULT": "drop-row"})
    assert len(machine.folder_names()) == 2 and run.file.hash in machine.sources(run)
    machine.remove_uefilogs()
    machine.keeper()

    assert machine.folder_names() == [run.folder_name]
    assert machine.log(run) == run.log()
    machine.check()


def test_F2_unreadable_index_file(machine):
    """UEFI_Index.txt is one more thing that can fail (SPEC 1): the file logs are archived with `-` as index,
    the memory log and the error records are processed."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    index = machine.uefilogs / "UEFI_Index.txt"
    index.unlink()
    index.mkdir()  # cannot be read as a file, whoever runs the keeper
    res = machine.keeper(expect=1)

    assert len(res.starting("error: ")) == 1 and res.starting("error: bootefi: ")
    assert machine.bootefi_seen() == [machine.seen_line(res, run.file, 1, "-")]
    assert machine.log(run) == run.log([rec])
    assert machine.records_in_firmware() == []
    machine.check()


@not_root
def test_F1_absorbed_folder_that_could_not_be_removed_goes_at_a_later_boot(machine):
    run, rec, file_only = _file_only_then_readable(machine)
    file_only.chmod(0o555)
    res = machine.keeper(expect=1)
    left = [p for p in machine.merged_root.iterdir() if f"fileonly-{run.file.hash}" in p.name]
    assert res.starting("error: purge: ") and len(left) == 1 and left[0].is_dir()
    assert left[0].name == file_only.name + ".removing"  # renamed first (SPEC 4): never half a run folder
    left[0].chmod(0o755)

    later = machine.firmware_run(boot_lines("b"), reset=T0 + timedelta(hours=1))  # this boot's memory log is gone
    machine.keeper()
    assert machine.folder_names() == [run.folder_name, later.folder_name]
    assert machine.log(run) == run.log([rec])
    machine.check()  # H4: one folder per run, one row per record


def test_F6_boot_id_with_dashes_or_capitals(machine):
    """The boot id names folders that later runs must find again: it is always 32 lower-case hex digits."""
    run = machine.firmware_run(boot_lines("a"))
    dashed = str(uuid.UUID(run.boot_id)).upper()
    for _ in range(2):
        machine.keeper(boot_id=dashed)
        assert machine.folder_names() == [run.folder_name]
    assert machine.log(run) == run.log()
    assert {line[1] for line in machine.bootefi_seen()} == {run.boot_id}
    machine.check()


def test_F6_boot_id_that_is_not_one(machine):
    machine.firmware_run(boot_lines("a"))
    res = machine.keeper(boot_id="not-a-boot-id", expect=1)

    assert len(res.lines) == 1 and res.starting("error: keeper: ")
    assert not machine.memory_root.exists() and not machine.merged_root.exists() and not machine.bootefi_root.exists()
    machine.check()
