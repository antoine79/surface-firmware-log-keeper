"""Group A of TEST-SCENARIOS.md: one boot, one or both sources."""
import struct
from datetime import datetime, timedelta

from builders import (COLUMNS, FREQ, HEADER_SIZE, PHASE_DXE, Entry, FileLog, L, MemLog, boot_lines, esrt_text,
                      fpdt_text, log_bytes, merge, record, tree)

T0 = datetime(2026, 11, 20, 12, 0, 0)
FOUR = [L(374, "SEC start"), L(376, "PcdPeim.efi", 0x80000004), L(376, "", 0x80000004), L(5000, "Boot loader", 0x80000040)]


def test_A1_memory_only(machine):
    machine.remove_uefilogs()
    run = machine.firmware_run(FOUR, reset=T0)
    res = machine.keeper()

    name = f"2026-11-20T120000_{run.boot_id}"
    assert machine.folder_names() == [name]
    folder = machine.merged_root / name
    assert (folder / "firmware.log").read_text() == COLUMNS + (
        "2026-11-20T12:00:00.374\t0.374\t0\tL80000000\tmemory\t1\t-\tSEC start\n"
        "2026-11-20T12:00:00.376\t0.376\t0\tL80000004\tmemory\t2\t-\tPcdPeim.efi\n"
        "2026-11-20T12:00:00.376\t0.376\t0\tL80000004\tmemory\t3\t-\t\n"
        "2026-11-20T12:00:05.000\t5.000\t0\tL80000040\tmemory\t4\t-\tBoot loader\n")
    assert (folder / "sources.tsv").read_text() == f"memory\t{run.mem.hash}\t4\t-\t-\n"
    assert (folder / "fpdt-boot.txt").read_text() == fpdt_text(run.n)
    assert (folder / "esrt.txt").read_text() == esrt_text()
    assert res.merged(run.boot_id) == (name, 4, 0, 0)
    assert len(res.starting("skipped bootefi: ")) == 1
    assert machine.raw_logs() == set()
    machine.check()


def test_A1_memory_newer_header_and_entry_layout(machine):
    """The header with 32-bit offsets and the ALM2 entries of newer Project Mu firmware."""
    machine.remove_uefilogs()
    entries = [Entry(374 * 1000, b"old style entry\r\n", 0x80000000, "ALMS"),
               Entry(500 * 1000, b"new style entry\r\n", 0x00000040, "ALM2"),
               Entry(600 * 1000, b"two lines\r\nin one new entry\r\n", 0x80000004, "ALM2"),
               Entry(700 * 1000, b"odd length!\r\n", 0x80000000, "ALMS")]
    mem = MemLog(entries, clock=T0 + timedelta(seconds=2), layout="offsets")
    run = machine.firmware_run(memlog=mem)
    machine.keeper()

    assert machine.log(run) == run.log()
    assert [(r.uptime, r.level, r.message) for r in machine.rows(run)] == [
        ("0.374", "L80000000", "old style entry"), ("0.500", "L00000040", "new style entry"),
        ("0.600", "L80000004", "two lines"), ("0.600", "L80000004", "in one new entry"),
        ("0.700", "L80000000", "odd length!")]
    machine.check()


def test_A2_both_copies_identical(machine):
    run = machine.firmware_run(FOUR, reset=T0)
    res = machine.keeper()

    name = f"2026-11-20T120000_{run.boot_id}"
    assert machine.folder_names() == [name]
    assert machine.log(run).decode() == COLUMNS + (
        "2026-11-20T12:00:00.374\t0.374\t0\tL80000000\tboth\t1\t1\tSEC start\n"
        "2026-11-20T12:00:00.376\t0.376\t0\tL80000004\tboth\t2\t2\tPcdPeim.efi\n"
        "2026-11-20T12:00:00.376\t0.376\t0\tL80000004\tboth\t3\t3\t\n"
        "2026-11-20T12:00:05.000\t5.000\t0\tL80000040\tboth\t4\t4\tBoot loader\n")
    assert machine.sources(run) == (f"memory\t{run.mem.hash}\t4\t-\t-\n"
                                    f"bootefi\t{run.file.hash}\t4\tUEFI_Log1.txt\tmerged\n")
    # the raw copy is the file without its padding, and nothing else
    content = (machine.bootefi_root / f"{run.file.hash}.txt").read_bytes()
    assert content == run.file.content
    assert content.startswith(b"00:00:00.374 : SEC start\r\n00:00:00.376 : PcdPeim.efi\r\n00:00:00.376 : \r\n")
    assert content.endswith(b"00:00:05.000 : Boot loader\r\n\r\n\r\n === END_OF_LOG === @ === 2026/11/20 12:00:05 ===")
    assert machine.bootefi_seen() == [(res.utc, run.boot_id, "UEFI_Log1.txt", run.file.hash, str(len(content)), "0",
                                       "yes", "2026/11/20 12:00:05", "1")]
    assert res.utc == "2026-11-21T050000Z"
    assert f"archived file log {run.file.hash} from UEFI_Log1.txt" in res.lines
    assert res.merged(run.boot_id) == (name, 4, 4, 0)
    machine.check()


def test_A2_ticks_are_truncated_to_the_millisecond(machine):
    """The firmware writes the file log with truncated milliseconds, and stamps a line with the entry that ends it.

    Same shape as the Surface Book 2 logs: a 2.4 GHz timer, a module name and its line break in two entries.
    """
    freq = 2_400_000_123

    def tick(seconds):
        return int(seconds * freq)

    entries = [Entry(tick(0.3749), b"SEC start\r\n"),
               Entry(tick(0.37595), b"PcdPeim.efi", 0x80000004), Entry(tick(0.37602), b"\r\n", 0x80000004),
               Entry(tick(1.92694), b"Loading driver ", 0x80000004), Entry(tick(1.92697), b"Foo.efi", 0x80000004),
               Entry(tick(1.92703), b"\r\n", 0x80000004),
               Entry(tick(4.9999), b"Boot loader\r\n")]
    mem = MemLog(entries, clock=T0 + timedelta(seconds=2), clock_tick=tick(2.0305), freq=freq)
    file_lines = [L(374, "SEC start"), L(376, "PcdPeim.efi"), L(1927, "Loading driver Foo.efi"), L(4999, "Boot loader")]
    run = machine.firmware_run(memlog=mem, file_lines=file_lines, reset=T0)
    machine.keeper()

    rows = machine.rows(run)
    assert [(r.uptime, r.source, r.mem_line, r.file_line, r.message) for r in rows] == [
        ("0.374", "both", "1", "1", "SEC start"), ("0.376", "both", "2", "2", "PcdPeim.efi"),
        ("1.927", "both", "3", "3", "Loading driver Foo.efi"), ("4.999", "both", "4", "4", "Boot loader")]
    # clock read 12:00:02 at 2.0305 s: a line at 0.3749 s is 1.6556 s earlier, rounded down to the millisecond
    assert rows[0].time == "2026-11-20T12:00:00.344"
    assert machine.log(run) == run.log()
    machine.check()


def test_A3_memory_has_lines_after_the_hand_over(machine):
    late = [L(27882, "SecurityLock"), L(27890, "Exit boot services", 0x80000040)]
    run = machine.firmware_run(boot_lines("a"), late=late)
    res = machine.keeper()

    assert [r.source for r in machine.rows(run)] == ["both"] * 9 + ["memory"] * 2
    assert machine.log(run) == run.log()
    assert res.merged(run.boot_id) == (run.folder_name, 11, 9, 0)
    machine.check()


def test_A4_one_line_differs(machine):
    in_memory = [L(374, "SEC start"), L(1000, "Driver A loaded"), L(5000, "Boot loader")]
    in_file = [L(374, "SEC start"), L(1000, "Driver B loaded"), L(5000, "Boot loader")]
    run = machine.firmware_run(in_memory, file_lines=in_file, reset=T0)
    res = machine.keeper()

    # not every line of the file is in the memory log: the file is another run (SPEC 3.1)
    own = f"fileonly-{run.file.hash}"
    assert sorted(name.split("_")[1] for name in machine.folder_names()) == sorted([run.boot_id, own])
    assert machine.log(run.boot_id).decode() == COLUMNS + (
        "2026-11-20T12:00:00.374\t0.374\t0\tL80000000\tmemory\t1\t-\tSEC start\n"
        "2026-11-20T12:00:01.000\t1.000\t0\tL80000000\tmemory\t2\t-\tDriver A loaded\n"
        "2026-11-20T12:00:05.000\t5.000\t0\tL80000000\tmemory\t3\t-\tBoot loader\n")
    assert machine.log(own).decode() == COLUMNS + (
        "2026-11-20T12:00:00.374\t0.374\t0\t-\tbootefi\t-\t1\tSEC start\n"
        "2026-11-20T12:00:01.000\t1.000\t0\t-\tbootefi\t-\t2\tDriver B loaded\n"
        "2026-11-20T12:00:05.000\t5.000\t0\t-\tbootefi\t-\t3\tBoot loader\n")
    assert res.merged(run.boot_id)[1:] == (3, 0, 0) and res.merged(own)[1:] == (0, 3, 0)
    machine.check()  # H6: the difference is stored, never commented


def test_A5_memory_entry_spanning_several_lines(machine):
    entries = [Entry(1_000_000, b"alpha\nbeta\r\ngamma\r\n"),
               Entry(2_000_000, b"Loading driver ", 0x80000004),  # a line built from three entries
               Entry(2_000_900, b"Foo.efi", 0x80000004),
               Entry(2_001_200, b"\r\n", 0x80000040),
               Entry(3_000_000, b"no line break at the end")]
    mem = MemLog(entries, clock=T0 + timedelta(seconds=2))
    in_file = [L(1000, "alpha"), L(1000, "beta"), L(1000, "gamma"), L(2001, "Loading driver Foo.efi"),
               L(3000, "no line break at the end")]
    run = machine.firmware_run(memlog=mem, file_lines=in_file, reset=T0)
    machine.keeper()

    assert machine.log(run).decode() == COLUMNS + (
        "2026-11-20T12:00:01.000\t1.000\t0\tL80000000\tboth\t1\t1\talpha\n"
        "2026-11-20T12:00:01.000\t1.000\t0\tL80000000\tboth\t2\t2\tbeta\n"
        "2026-11-20T12:00:01.000\t1.000\t0\tL80000000\tboth\t3\t3\tgamma\n"
        "2026-11-20T12:00:02.001\t2.001\t0\tL80000040\tboth\t4\t4\tLoading driver Foo.efi\n"
        "2026-11-20T12:00:03.000\t3.000\t0\tL80000000\tboth\t5\t5\tno line break at the end\n")
    machine.check()


def test_A6_row_key_occurrences(machine):
    in_memory = [L(100, "first"), L(100, "second"), L(100, ""), L(100, ""),
                 L(200, "dup"), L(200, "dup"), L(200, "dup"), L(300, "end")]
    in_file = in_memory[:6] + [L(300, "end")]  # the repeated line twice only
    run = machine.firmware_run(in_memory, file_lines=in_file, reset=T0)
    machine.keeper()

    assert machine.log(run).decode() == COLUMNS + (
        "2026-11-20T12:00:00.100\t0.100\t0\tL80000000\tboth\t1\t1\tfirst\n"
        "2026-11-20T12:00:00.100\t0.100\t0\tL80000000\tboth\t2\t2\tsecond\n"
        "2026-11-20T12:00:00.100\t0.100\t0\tL80000000\tboth\t3\t3\t\n"
        "2026-11-20T12:00:00.100\t0.100\t0\tL80000000\tboth\t4\t4\t\n"
        "2026-11-20T12:00:00.200\t0.200\t0\tL80000000\tboth\t5\t5\tdup\n"
        "2026-11-20T12:00:00.200\t0.200\t0\tL80000000\tboth\t6\t6\tdup\n"
        "2026-11-20T12:00:00.200\t0.200\t0\tL80000000\tmemory\t7\t-\tdup\n"
        "2026-11-20T12:00:00.300\t0.300\t0\tL80000000\tboth\t8\t7\tend\n")
    machine.check()


def test_A6_tabs_and_trailing_spaces_do_not_split_a_line(machine):
    """The message column has tabs as spaces and no space, tab, CR or LF at its end; the row key uses that same
    text. Nothing else is removed: a form feed or a non-breaking space at the end stays (SPEC 3.2)."""
    lines = [L(374, "SEC start"), L(500, "name\tvalue"), L(600, "padded   "), L(700, "  indented"),
             L(800, "form feed\x0c"), L(900, "non-breaking space\xa0"), L(5000, "Boot loader")]
    run = machine.firmware_run(lines, reset=T0)
    machine.keeper()

    assert [(r.source, r.message) for r in machine.rows(run)] == [
        ("both", "SEC start"), ("both", "name value"), ("both", "padded"), ("both", "  indented"),
        ("both", "form feed\x0c"), ("both", "non-breaking space\xa0"), ("both", "Boot loader")]
    machine.check()


def test_A7_file_only_run_that_never_reached_linux(machine):
    hung = machine.firmware_run([L(374, "SEC start"), L(376, "PcdPeim.efi"), L(5000, "Boot loader")],
                                reset=datetime(2026, 11, 20, 11, 0, 0), linux=False)
    later = machine.firmware_run(boot_lines("later"), reset=T0)
    res = machine.keeper()

    name = f"2026-11-20T110000_fileonly-{hung.file.hash}"
    assert machine.folder_names() == [name, later.folder_name]
    assert (machine.merged_root / name / "firmware.log").read_text() == COLUMNS + (
        "2026-11-20T11:00:00.374\t0.374\t0\t-\tbootefi\t-\t1\tSEC start\n"
        "2026-11-20T11:00:00.376\t0.376\t0\t-\tbootefi\t-\t2\tPcdPeim.efi\n"
        "2026-11-20T11:00:05.000\t5.000\t0\t-\tbootefi\t-\t3\tBoot loader\n")
    assert (machine.merged_root / name / "sources.tsv").read_text() == f"bootefi\t{hung.file.hash}\t3\tUEFI_Log1.txt\tmerged\n"
    assert sorted(p.name for p in (machine.merged_root / name).iterdir()) == ["firmware.log", "sources.tsv"]
    assert res.merged(hung.key) == (name, 0, 3, 0)
    machine.check()  # H6: nothing is flagged


def _a8_story(machine):
    in_memory = [L(500, "start"), L(1000, "a"), L(1000, "memory only"), L(1000, "b"), L(2000, "end")]
    in_file = [L(500, "start"), L(1000, "a"), L(1000, "b"), L(2000, "end")]
    run = machine.firmware_run(in_memory, file_lines=in_file, reset=T0)
    records = [record(T0 + timedelta(seconds=1), 0x30), record(T0 + timedelta(seconds=1), 0x20)]
    for rec in records:
        machine.add_record(rec)
    machine.keeper()
    return run, records


def test_A8_sort_order(machines):
    machine, twin = machines(), machines()
    run, records = _a8_story(machine)

    rows = machine.rows(run)
    assert [(r.uptime, r.source, r.mem_line, r.file_line) for r in rows] == [
        ("0.500", "both", "1", "1"),
        ("1.000", "both", "2", "2"),  # same uptime: by source name, then by line number or record id
        ("1.000", "both", "4", "3"),
        ("1.000", "errrec", "-", "-"),
        ("1.000", "errrec", "-", "-"),
        ("1.000", "memory", "3", "-"),
        ("2.000", "both", "5", "4")]
    assert [r.message for r in rows[3:5]] == [records[1].message, records[0].message]  # record 0x20 before 0x30
    assert machine.log(run) == run.log(records)

    # the same inputs give the same archive, byte for byte: on a second machine, and at a second keeper run
    _a8_story(twin)
    assert tree(twin.merged_root) == tree(machine.merged_root)
    assert tree(twin.bootefi_root) == tree(machine.bootefi_root)
    before = machine.log(run)
    machine.keeper()
    assert machine.log(run) == before
    machine.check()
    twin.check()


def test_A9_segment_safeguard(machine):
    lines = [L(1000, "one"), L(2000, "two"), L(3000, "three"), L(500, "again one"), L(1500, "again two")]
    end = FileLog(lines, T0 + timedelta(seconds=1))
    run = machine.firmware_run(lines, filelog=end, reset=T0)
    machine.keeper()

    assert machine.log(run).decode() == COLUMNS + (
        "2026-11-20T12:00:01.000\t1.000\t0\tL80000000\tboth\t1\t1\tone\n"
        "2026-11-20T12:00:02.000\t2.000\t0\tL80000000\tboth\t2\t2\ttwo\n"
        "2026-11-20T12:00:03.000\t3.000\t0\tL80000000\tboth\t3\t3\tthree\n"
        "2026-11-20T12:00:00.500\t0.500\t1\tL80000000\tboth\t4\t4\tagain one\n"
        "2026-11-20T12:00:01.500\t1.500\t1\tL80000000\tboth\t5\t5\tagain two\n")
    assert machine.folder(run).name == f"2026-11-20T120001_{run.boot_id}"
    machine.check()


def test_A9_segment_in_one_copy_only(machine):
    """Memory only: the uptime going back starts segment 1, and the rows stay in two blocks."""
    machine.remove_uefilogs()
    run = machine.firmware_run([L(1000, "one"), L(3000, "three"), L(500, "again one"), L(3000, "three")], reset=T0)
    machine.keeper()

    assert [(r.seg, r.uptime, r.mem_line) for r in machine.rows(run)] == [
        (0, "1.000", "1"), (0, "3.000", "2"), (1, "0.500", "3"), (1, "3.000", "4")]
    machine.check()


def test_A10_time_from_the_memory_header(machine):
    """Clock read 2026-03-05 07:04:09 at tick 2.5 s: the reset was at 07:04:06.500."""
    lines = [L(374, "SEC start"), L(5000, "Boot loader")]
    mem = MemLog.from_lines(lines, datetime(2026, 3, 5, 7, 4, 7), clock_tick=5 * FREQ // 2)
    run = machine.firmware_run(lines, memlog=mem, reset=datetime(2026, 3, 5, 7, 4, 6, 500000))
    res = machine.keeper()

    assert machine.log(run).decode() == COLUMNS + (
        "2026-03-05T07:04:06.874\t0.374\t0\tL80000000\tboth\t1\t1\tSEC start\n"
        "2026-03-05T07:04:11.500\t5.000\t0\tL80000000\tboth\t2\t2\tBoot loader\n")
    assert machine.folder(run).name == f"2026-03-05T070406_{run.boot_id}"
    # the END_OF_LOG clock is recorded as the firmware wrote it
    assert machine.bootefi_seen()[0][7] == "2026/3/5 7:04:11"
    assert res.merged(run.boot_id)[0] == machine.folder(run).name
    machine.check()


def test_A10_time_derived_from_end_of_log(machine):
    """File only: END_OF_LOG clock minus the uptime of the last line, plus the line's uptime."""
    lines = [L(374, "SEC start"), L(5250, "Boot loader")]
    hung = machine.firmware_run(filelog=FileLog(lines, datetime(2026, 3, 5, 7, 4, 9)), linux=False)
    machine.firmware_run(boot_lines("later"))
    machine.keeper()

    assert machine.log(hung).decode() == COLUMNS + (
        "2026-03-05T07:04:04.124\t0.374\t0\t-\tbootefi\t-\t1\tSEC start\n"
        "2026-03-05T07:04:09.000\t5.250\t0\t-\tbootefi\t-\t2\tBoot loader\n")
    assert machine.folder(hung).name == f"2026-03-05T070404_fileonly-{hung.file.hash}"
    machine.check()


def test_A10_no_time_without_end_of_log(machine):
    """File only and never flushed: no clock at all. The folder is named after the keeper run that found it."""
    hung = machine.firmware_run([L(374, "SEC start"), L(5000, "Boot loader")], linux=False, complete=False)
    machine.firmware_run(boot_lines("later"))
    res = machine.keeper()

    assert machine.log(hung).decode() == COLUMNS + ("-\t0.374\t0\t-\tbootefi\t-\t1\tSEC start\n"
                                                    "-\t5.000\t0\t-\tbootefi\t-\t2\tBoot loader\n")
    assert res.now == datetime(2026, 11, 21, 5, 0, 0)
    assert machine.folder(hung).name == f"2026-11-21T050000_fileonly-{hung.file.hash}"
    machine.check()


def test_A10_reset_time_when_the_clock_tick_is_between_two_milliseconds(machine):
    """The reset time is the first memory row's time minus its uptime, each rounded down on its own (SPEC 3.2).

    Clock read 12:00:02 at tick 2.0005 s. The first line, at tick 0.3746 s, is 1.6259 s earlier: 12:00:00.374,
    and its uptime is 0.374, so the reset time is 12:00:00.000 and not 11:59:59.999 (the clock minus 2.0005 s).
    """
    entries = [Entry(374_600, b"first\r\n"), Entry(900_100, b"second\r\n"), Entry(4_500_000, b"last\r\n")]
    mem = MemLog(entries, clock=T0 + timedelta(seconds=2), clock_tick=2_000_500)
    in_file = [L(374, "first"), L(900, "second"), L(4500, "last")]
    run = machine.firmware_run(memlog=mem, file_lines=in_file, reset=T0)
    rec = record(T0 + timedelta(seconds=3))
    machine.add_record(rec)
    machine.keeper()

    assert machine.log(run).decode() == COLUMNS + (
        "2026-11-20T12:00:00.374\t0.374\t0\tL80000000\tboth\t1\t1\tfirst\n"
        "2026-11-20T12:00:00.899\t0.900\t0\tL80000000\tboth\t2\t2\tsecond\n"
        f"2026-11-20T12:00:03.000\t3.000\t0\t-\terrrec\t-\t-\t{rec.message}\n"
        "2026-11-20T12:00:04.499\t4.500\t0\tL80000000\tboth\t3\t3\tlast\n")
    assert machine.log(run) == run.log([rec])
    machine.check()


def test_A11_file_log_with_phase_and_level_tags(machine):
    """Newer Project Mu firmware writes a phase tag and a level tag before each line of the file log, and only
    there. The two copies are paired without the tags; the row holds the text of the memory copy."""
    lines = [L(374, "SEC start", 0x80000000), L(500, "Loading driver", 0x80000004), L(600, "", 0x40),
             L(700, "[Bds] booting", 0x40), L(800, "no known level bit", 0x08000000), L(5000, "Boot loader", 0x40)]
    entries = [Entry(x.ms * 1000, (x.text + "\r\n").encode(), x.level, "ALM2") for x in lines]
    mem = MemLog(entries, clock=T0 + timedelta(seconds=2), layout="offsets")
    run = machine.firmware_run(lines, memlog=mem, reset=T0, phase=PHASE_DXE)
    res = machine.keeper()

    assert run.file.content.startswith(b"00:00:00.374 : [DXE  ] [ERR ] SEC start\r\n"
                                       b"00:00:00.500 : [DXE  ] [LOAD] Loading driver\r\n"
                                       b"00:00:00.600 : [DXE  ] [INFO] \r\n"
                                       b"00:00:00.700 : [DXE  ] [INFO] [Bds] booting\r\n"
                                       b"00:00:00.800 : [DXE  ] no known level bit\r\n")
    assert machine.folder_names() == [run.folder_name]
    assert [(r.source, r.mem_line, r.file_line, r.message) for r in machine.rows(run)] == [
        ("both", str(n), str(n), x.text) for n, x in enumerate(lines, 1)]
    assert machine.log(run) == run.log()
    assert res.merged(run.boot_id) == (run.folder_name, 6, 6, 0)
    assert (machine.bootefi_root / f"{run.file.hash}.txt").read_bytes() == run.file.content  # the tags stay in the raw copy
    machine.check()


def test_A11_level_tag_only(machine):
    """Entries of the older type have no phase: the file has the level tag alone, and is paired without it."""
    in_memory = [L(374, "SEC start", 0x40), L(5000, "Boot loader", 0x2), L(5100, "Exit", 0x40)]
    run = machine.firmware_run(in_memory, reset=T0, phase="")
    machine.keeper()

    assert run.file.content.startswith(b"00:00:00.374 : [INFO] SEC start\r\n00:00:05.000 : [WARN] Boot loader")
    assert [(r.source, r.message) for r in machine.rows(run)] == [("both", x.text) for x in in_memory]
    assert machine.log(run) == run.log()
    machine.check()


def test_A11_text_like_a_tag_after_a_line_without_level_tag(machine):
    """The tags are told by their shape: a text that begins like a level tag, on a line the firmware gave no level
    tag, loses that beginning too. Then not every line is in the memory log, either way: the file is another run,
    with its text as it is, and the boot's folder holds its memory lines."""
    in_memory = [L(374, "SEC start", 0x40), L(500, "[HDD0] ready", 0x08000000), L(5000, "Boot loader", 0x40)]
    run = machine.firmware_run(in_memory, reset=T0, phase="")
    machine.keeper()

    own = f"fileonly-{run.file.hash}"
    assert {r.source for r in machine.rows(run.boot_id)} == {"memory"}
    assert [r.message for r in machine.rows(own)] == ["[INFO] SEC start", "[HDD0] ready", "[INFO] Boot loader"]
    assert machine.log(own) == log_bytes(merge(None, run.file))
    machine.check()


def test_A11_tagged_file_only_run(machine):
    """Without a memory log there is nothing to pair: every row keeps the file's text as it is."""
    hung = machine.firmware_run([L(374, "SEC start", 0x40), L(5000, "Boot loader", 0x40)], reset=T0, linux=False,
                                phase=PHASE_DXE)
    machine.firmware_run(boot_lines("later"), phase=PHASE_DXE)
    machine.keeper()

    assert [(r.source, r.message) for r in machine.rows(hung)] == [
        ("bootefi", "[DXE  ] [INFO] SEC start"), ("bootefi", "[DXE  ] [INFO] Boot loader")]
    assert machine.log(hung) == hung.log()
    machine.check()


class UnknownEntry(MemLog):
    """A memory log whose third entry has a signature the decoder does not know (a later entry layout)."""

    def buffer(self) -> bytes:
        data = bytearray(super().buffer())
        at = HEADER_SIZE
        for _ in range(2):
            at += (18 + struct.unpack_from("<H", data, at + 16)[0] + 7) & ~7
        data[at:at + 4] = b"ALM9"
        return bytes(data)


def test_A12_memory_dump_with_an_entry_the_decoder_does_not_know(machine):
    """The memory log has no raw copy in the archive: a dump with bytes that became no line is kept raw in the
    run folder before staging is emptied, and the run says so (SPEC 1.1)."""
    machine.remove_uefilogs()
    lines = boot_lines("a")
    mem = UnknownEntry.from_lines(lines, T0)
    run = machine.firmware_run(memlog=mem, reset=T0)
    res = machine.keeper(expect=1)

    decoded = MemLog.from_lines(lines[:2] + lines[3:], T0)  # what the decoder reads: every entry but the third
    machine.memory_seen[run.boot_id] = decoded
    assert machine.log(run) == log_bytes(merge(decoded))
    assert machine.sources(run) == f"memory\t{mem.hash}\t8\t-\t-\n"
    kept = machine.folder(run) / f"advlog-{mem.hash}.bin"
    assert kept.read_bytes() == mem.buffer() and b"a step 2" in kept.read_bytes()
    errors = res.starting("error: ")
    assert len(errors) == 1 and errors[0].startswith("error: memory: ")
    assert kept.name in errors[0] and "32" in errors[0].split()  # the entry skipped: 18 + 10 bytes, padded to 8
    assert list(machine.memory_root.iterdir()) == []  # the staging folder went only after that copy
    machine.check()
