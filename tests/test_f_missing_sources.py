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


@pytest.mark.parametrize("block, unused", [(1 << 20, 0), (100, 0), (100, 5000)],
                         ids=["one block", "many blocks", "whole buffer handed out"])
def test_F1_memory_log_through_the_log_variables(machine, block, unused):
    """SPEC 1.1: /dev/mem refused (a kernel in lockdown mode), the same log read through the variables V0, V1, ..."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    machine.devmem.unlink()
    run.mem.write_variables(machine.efivars, block=block, unused=unused)
    res = machine.keeper()

    assert res.starting("skipped memory: ") == [] and res.starting("error: ") == []
    assert machine.folder_names() == [run.folder_name]
    assert machine.log(run) == run.log()
    machine.check()


def test_F1_log_variables_that_end_early_are_an_error(machine):
    """A last block missing: the log is not cut short, the source fails and the others still run."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    machine.devmem.unlink()
    run.mem.write_variables(machine.efivars, block=100, drop_last=True)
    machine.memory_ok = False
    res = machine.keeper(expect=1)

    assert len(res.starting("error: memory: ")) == 1 and "short of the write position" in res.out
    assert machine.log(f"fileonly-{run.file.hash}") == log_bytes(merge(None, run.file))
    machine.check()


def test_F1_memory_log_readable_at_a_later_run(machine):
    """SPEC 2.1: a fileonly- folder keeps its file log and its records when the memory log of that run is read
    later; the boot's folder, named after the first keeper run, gets the memory lines."""
    run = machine.firmware_run(boot_lines("a"), reset=T0)
    rec = record(T0 + 2 * S)
    machine.add_record(rec)
    locator = (machine.efivars / LOCATOR_VAR).read_bytes()
    _lose_memory(machine, "no locator variable")
    first = machine.keeper()
    file_only = machine.folder(f"fileonly-{run.file.hash}")
    log = (file_only / "firmware.log").read_bytes()

    (machine.efivars / LOCATOR_VAR).write_bytes(locator)
    machine.keeper()
    assert machine.folder_names() == sorted([file_only.name, f"{label(first.now)}_{run.boot_id}"])
    assert (file_only / "firmware.log").read_bytes() == log
    assert machine.log(run.boot_id) == log_bytes(merge(run.mem))
    assert machine.sources(run.boot_id) == f"memory\t{run.mem.hash}\t9\t-\t-\n"
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


@not_root
def test_F4_unreadable_timings_are_an_error_not_a_missing_source(machine):
    """A timings folder that cannot be listed is reported as an error of its own; the other sources still run."""
    run = machine.firmware_run(boot_lines("a"))
    machine.fpdt.chmod(0)
    try:
        res = machine.keeper(expect=1)
    finally:
        machine.fpdt.chmod(0o755)

    assert len(res.starting("error: fpdt: ")) == 1 and res.starting("skipped fpdt") == []
    assert sorted(p.name for p in machine.folder(run).iterdir()) == ["esrt.txt", "firmware.log", "sources.tsv"]
    assert machine.log(run) == run.log()
    machine.check()
