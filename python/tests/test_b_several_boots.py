"""Group B of TEST-SCENARIOS.md: several boots."""
from datetime import datetime, timedelta

import pytest

from builders import Entry, FileLog, L, MemLog, boot_lines, log_bytes, merge, record, tree

T0 = datetime(2026, 11, 20, 12, 0, 0)


def test_B1_linux_then_hang_then_linux(machine):
    first = machine.firmware_run(boot_lines("first"))
    machine.keeper()
    hung = machine.firmware_run(boot_lines("hung"), linux=False)
    third = machine.firmware_run(boot_lines("third"))
    res = machine.keeper()

    assert machine.folder_names() == [first.folder_name, hung.folder_name, third.folder_name]
    assert hung.key == f"fileonly-{hung.file.hash}"
    for run in (first, hung, third):
        assert machine.log(run) == run.log()
    assert {r.source for r in machine.rows(hung)} == {"bootefi"}
    assert {r.source for r in machine.rows(third)} == {"both"}
    # the first boot's file is still in its slot: seen again, archived once
    assert sorted(res.starting("archived file log ")) == sorted(
        f"archived file log {run.file.hash} from UEFI_Log{run.slot}.txt" for run in (hung, third))
    assert machine.raw_logs() == {first.file.hash, hung.file.hash, third.file.hash}
    machine.check()


def test_B2_same_nine_files_seen_at_nine_runs(machine):
    old = [machine.firmware_run(boot_lines(f"old{i}"), linux=False) for i in range(8)]
    boots = [machine.firmware_run(boot_lines("ninth"))]
    results = [machine.keeper()]
    files = [(run.slot, run.file) for run in old + boots]
    assert len(results[0].starting("archived file log ")) == 9

    for i in range(8):  # eight more Linux boots; the firmware writes no new file
        boots.append(machine.firmware_run(boot_lines(f"more{i}"), write_file=False))
        results.append(machine.keeper())
        assert results[-1].starting("archived file log ") == []
        assert machine.raw_logs() == {file.hash for _, file in files}
        assert len(machine.folder_names()) == 8 + len(boots)

    assert sorted(machine.folder_names()) == sorted(run.folder_name for run in old + boots)
    assert machine.bootefi_seen() == [machine.seen_line(res, file, slot, 9) for res in results for slot, file in files]
    assert len(machine.bootefi_seen()) == 81
    machine.check()


def test_B3_rotation_overwrites_a_slot(machine):
    runs = [machine.firmware_run(boot_lines(f"run{i}"), linux=False) for i in range(8)]
    runs.append(machine.firmware_run(boot_lines("ninth")))
    machine.keeper()
    tenth = machine.firmware_run(boot_lines("tenth"))
    res = machine.keeper()

    assert tenth.slot == runs[0].slot == 1
    assert res.starting("archived file log ") == [f"archived file log {tenth.file.hash} from UEFI_Log1.txt"]
    assert machine.raw_logs() == {run.file.hash for run in runs + [tenth]}
    assert (machine.bootefi_root / f"{runs[0].file.hash}.txt").read_bytes() == runs[0].file.content
    assert sorted(machine.folder_names()) == sorted(run.folder_name for run in runs + [tenth])
    assert machine.log(runs[0]) == runs[0].log()
    assert machine.bootefi_seen()[9:] == [machine.seen_line(res, run.file, run.slot, 1) for run in [tenth] + runs[1:]]
    machine.check()


def test_B4_two_files_match_the_memory_log(machine):
    lines = boot_lines("x", n=20)
    end = T0 + timedelta(seconds=1)
    short = FileLog(lines[:10], end)
    machine.write_file(short)
    run = machine.firmware_run(lines, file_lines=lines[:14], reset=T0)
    res = machine.keeper()

    assert machine.folder_names() == [run.folder_name]
    assert machine.sources(run) == (f"memory\t{run.mem.hash}\t21\t-\t-\n"
                                    f"bootefi\t{run.file.hash}\t14\tUEFI_Log2.txt\tmerged\n"
                                    f"bootefi\t{short.hash}\t10\tUEFI_Log1.txt\textra\n")
    assert machine.log(run) == run.log()  # the longer file is the one merged
    assert machine.raw_logs() == {short.hash, run.file.hash}
    assert res.merged(run.boot_id) == (run.folder_name, 21, 14, 0)
    assert len(machine.bootefi_seen()) == 2
    machine.check()


def test_B5_two_boots_with_identical_first_lines(machine):
    common = [L(100, "SEC start"), L(102, "PcdPeim.efi"), L(102, "ReportStatusCodeRouterPei.efi"), L(103, "")]
    hung = machine.firmware_run(common + boot_lines("hung", n=5), linux=False)  # 4 of its 10 lines are common
    run = machine.firmware_run(common + boot_lines("now", n=5))
    machine.keeper()

    assert machine.folder_names() == [hung.folder_name, run.folder_name]
    assert machine.log(hung) == hung.log()
    assert {r.source for r in machine.rows(hung)} == {"bootefi"}
    assert machine.log(run) == run.log()
    assert {r.source for r in machine.rows(run)} == {"both"}
    machine.check()


def test_B5b_another_boot_with_the_same_power_on_time(machine):
    """Two runs at the same firmware-clock time (clock reset in between): told apart by content, not by time."""
    hung = machine.firmware_run(boot_lines("hung"), reset=T0, linux=False)
    run = machine.firmware_run(boot_lines("now"), reset=T0)
    machine.keeper()

    assert machine.folder_names() == [run.folder_name, hung.folder_name]
    assert run.folder_name.split("_")[0] == hung.folder_name.split("_")[0] == "2026-11-20T120000"
    assert machine.log(hung) == hung.log()
    assert machine.log(run) == run.log()
    machine.check()


def test_B6_keeper_run_twice_in_the_same_boot(machine):
    hung = machine.firmware_run(boot_lines("hung"), linux=False)
    run = machine.firmware_run(boot_lines("now"))
    rec = record(run.reset + timedelta(seconds=2))
    machine.add_record(rec)
    first = machine.keeper()
    merged, raw, records_seen = tree(machine.merged_root), machine.raw_logs(), machine.errrec_seen()
    second = machine.keeper()

    assert tree(machine.merged_root) == merged
    assert machine.raw_logs() == raw
    assert machine.errrec_seen() == records_seen
    assert second.starting("archived ") == [] and second.starting("deleted ") == []
    files = [(hung.slot, hung.file), (run.slot, run.file)]
    assert machine.bootefi_seen() == [machine.seen_line(res, file, slot, 2) for res in (first, second) for slot, file in files]
    assert machine.log(run) == run.log([rec])
    machine.check()


def test_B7_more_than_nine_boots_that_never_reached_linux(machine):
    hung = [machine.firmware_run(boot_lines(f"hung{i}"), linux=False) for i in range(12)]
    run = machine.firmware_run(boot_lines("now"))
    res = machine.keeper()

    survivors = hung[4:]  # the first four were overwritten: slots 1 to 4 now hold runs 10, 11, 12 and the current one
    assert run.slot == 4
    assert sorted(machine.folder_names()) == sorted(r.folder_name for r in survivors + [run])
    assert machine.raw_logs() == {r.file.hash for r in survivors + [run]}
    # the firmware's index value is recorded with every file; finding the gap is the report program's job
    by_slot = sorted(survivors + [run], key=lambda r: r.slot)
    assert machine.bootefi_seen() == [machine.seen_line(res, r.file, r.slot, 4) for r in by_slot]
    machine.check()


def test_B4_two_files_sharing_as_many_lines_the_lowest_hash_is_merged(machine):
    lines = boot_lines("x", n=20)
    copies = [FileLog(lines[:10], T0 + timedelta(seconds=n)) for n in (5, 8, 6, 7)]  # they differ by their last line
    for copy in copies:
        machine.write_file(copy)
    run = machine.firmware_run(lines, write_file=False, reset=T0)
    machine.keeper()

    slot = {copy.hash: n for n, copy in enumerate(copies, 1)}
    lowest, *others = sorted(slot)
    assert slot[lowest] == 2  # neither the first file seen nor the last: only the hash can have picked it
    assert machine.sources(run) == (
        f"memory\t{run.mem.hash}\t21\t-\t-\n" + f"bootefi\t{lowest}\t10\tUEFI_Log{slot[lowest]}.txt\tmerged\n"
        + "".join(f"bootefi\t{h}\t10\tUEFI_Log{slot[h]}.txt\textra\n" for h in others))
    assert machine.folder_names() == [run.folder_name]
    machine.check()


def _runtime_story(machine):
    """A boot whose memory log has lines after the hand-over, archived by a first keeper run."""
    machine.remove_uefilogs()
    lines = boot_lines("a") + [L(6000 + i, f"runtime line {i}") for i in range(5)]
    run = machine.firmware_run(lines)
    machine.keeper()
    assert machine.log(run) == run.log() and len(machine.rows(run)) == 14
    return run, lines


def test_B6_second_run_with_a_shorter_memory_log_keeps_every_row(machine):
    """The lines of an archived memory log exist nowhere else: a later dump that lacks some never replaces it."""
    run, lines = _runtime_story(machine)
    before = tree(machine.merged_root)
    MemLog.from_lines(lines[:6], run.reset).write(machine.devmem, machine.efivars)  # the buffer now reads shorter
    res = machine.keeper()

    assert tree(machine.merged_root) == before
    assert res.starting("merged run ") == [] and res.starting("error: ") == []
    machine.check()  # H5: the dump held nothing new, it is gone from staging


def test_B6_second_run_with_another_memory_log_keeps_the_first(machine):
    run, lines = _runtime_story(machine)
    before = tree(machine.merged_root)
    other = lines[:3] + [L(lines[3].ms, "another text")] + lines[4:]
    MemLog.from_lines(other, run.reset).write(machine.devmem, machine.efivars)
    res = machine.keeper(expect=1)

    assert tree(machine.merged_root) == before
    staging = machine.memory_root / f"{res.utc}_{run.boot_id}"
    assert [line for line in res.starting("error: memory: ") if str(staging) in line]
    assert (staging / "advlog.bin").is_file()  # kept: its lines are in no log
    machine.check()


def test_B6_second_run_with_a_longer_memory_log_replaces_the_first(machine):
    """The firmware goes on logging after the first keeper run, and ends a line that was unfinished then."""
    machine.remove_uefilogs()
    clock = T0 + timedelta(seconds=2)
    first = [Entry(374_000, b"SEC start\r\n"), Entry(5_000_000, b"Exit boot")]
    run = machine.firmware_run(memlog=MemLog(first, clock), reset=T0)
    machine.keeper()
    assert [(r.uptime, r.message) for r in machine.rows(run)] == [("0.374", "SEC start"), ("5.000", "Exit boot")]

    run.mem = MemLog(first + [Entry(5_000_400, b" services\r\n"), Entry(9_000_000, b"runtime line\r\n")], clock)
    run.mem.write(machine.devmem, machine.efivars)
    res = machine.keeper()
    assert [(r.uptime, r.message) for r in machine.rows(run)] == [
        ("0.374", "SEC start"), ("5.000", "Exit boot services"), ("9.000", "runtime line")]
    assert machine.log(run) == run.log()
    assert res.merged(run.boot_id) == (run.folder_name, 3, 0, 0)
    machine.check()


@pytest.mark.parametrize("complete", [True, False])
def test_B5c_another_boot_with_most_of_the_same_lines(machine, complete):
    """Two boots sharing 8 of their 10 lines to the millisecond, the older one known by its file only: two runs,
    because not every line of the file is in the other boot's memory log (SPEC 3.1). The clock plays no part: an
    END_OF_LOG an hour before that memory log's times, or none at all, gives the same result."""
    common = [L(100 + 10 * i, f"common {i}") for i in range(8)]
    hung = machine.firmware_run(common + [L(300, "hung 0"), L(310, "hung 1")], linux=False, reset=T0, complete=complete)
    run = machine.firmware_run(common + [L(300, "now 0"), L(6000, "now 1")], write_file=False,
                               reset=T0 + timedelta(hours=1))
    machine.keeper()

    assert sorted(name.split("_")[1] for name in machine.folder_names()) == sorted([hung.key, run.boot_id])
    assert machine.log(hung) == hung.log()
    assert machine.log(run) == run.log() and {r.source for r in machine.rows(run)} == {"memory"}
    machine.check()


def test_B4_file_whose_every_line_is_in_two_memory_logs_goes_to_the_lowest_boot_id(machine):
    """Two boots with the same first lines and the same clock (set back in between); the keeper run of the first
    was stopped after staging. The second boot's file holds only those first lines, so both memory logs have every
    one of its lines: it goes to the lowest boot id (SPEC 3.1), as an extra copy there. The archive is the same
    after one keeper run and after two."""
    common = [L(100 + 50 * i, f"common {i}") for i in range(8)]
    a = machine.firmware_run(common + [L(900, "own of a")], reset=T0)
    machine.stage(a, datetime(2026, 11, 21, 4, 0, 0))
    b = machine.firmware_run(common + [L(900, "own of b")], reset=T0, file_lines=common)
    assert a.boot_id < b.boot_id
    machine.keeper()

    assert sorted(machine.folder_names()) == sorted([a.folder_name, b.folder_name])
    assert machine.log(a) == a.log() and machine.log(b) == log_bytes(merge(b.mem))
    assert machine.sources(a) == (f"memory\t{a.mem.hash}\t9\t-\t-\n"
                                  f"bootefi\t{a.file.hash}\t9\tUEFI_Log1.txt\tmerged\n"
                                  f"bootefi\t{b.file.hash}\t8\tUEFI_Log2.txt\textra\n")
    first = tree(machine.merged_root)
    machine.keeper()
    assert tree(machine.merged_root) == first
    machine.check()


@pytest.mark.parametrize("second_read", ["shorter", "other lines"])
def test_B6_staged_dump_not_merged_yet_is_not_overwritten(machine, second_read):
    """The first keeper run failed its merge: the dump stays in staging and its lines are in no log. A second
    read of the same boot that lacks some of them does not replace it there (SPEC 3.1)."""
    machine.remove_uefilogs()
    lines = boot_lines("a") + [L(6000 + i, f"runtime line {i}") for i in range(2)]
    run = machine.firmware_run(lines)
    first = machine.keeper(expect=1, env={"FWLK_FAULT": "drop-row"})
    staging = machine.memory_root / f"{first.utc}_{run.boot_id}"
    assert (staging / "advlog.bin").read_bytes() == run.mem.buffer()

    other = lines[:6] if second_read == "shorter" else lines[:3] + [L(lines[3].ms, "another text")] + lines[4:]
    MemLog.from_lines(other, run.reset).write(machine.devmem, machine.efivars)
    res = machine.keeper(expect=0 if second_read == "shorter" else 1)
    assert machine.log(run) == run.log() and len(machine.rows(run)) == 11  # the staged dump is the one merged
    if second_read == "shorter":
        assert res.starting("error: ") == []
    else:
        assert [line for line in res.starting("error: memory: ") if str(staging) in line]
    machine.check()


@pytest.mark.parametrize("raw, written", [(b"4\r\n\x00\x00", "4"), (b"", "-"), (b" 1\t\t2 \n", "1 2"),
                                          (b"\xa07\xa0\n", "\xa07\xa0")])
def test_B7_index_value_as_written_in_seen_tsv(machine, raw, written):
    """SPEC section 2: runs of NUL, space, tab, CR and LF are one space, none at the ends, `-` if nothing is left.
    Any other byte stays, read as Latin-1."""
    run = machine.firmware_run(boot_lines("a"))
    (machine.uefilogs / "UEFI_Index.txt").write_bytes(raw)
    res = machine.keeper()

    assert machine.bootefi_seen() == [machine.seen_line(res, run.file, 1, written)]
    machine.check()
