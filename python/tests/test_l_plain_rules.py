"""Group L of TEST-SCENARIOS.md: the plain rules both keepers follow where a language could decide (SPEC 2, 4, 5,
6): numbers, white space, lines, names that are not UTF-8, orders, folder removal, an output nobody reads."""
import lzma
import os
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from builders import MemLog, boot_lines, legacy_folder, legacy_records, legacy_text, log_bytes, merge, record, tree
from conftest import keeper_bin, not_root

T0 = datetime(2026, 11, 20, 12, 0, 0)
S = timedelta(seconds=1)


# ----- L1: numbers, white space, lines, the time format

@pytest.mark.parametrize("value", ["28\x1c", " 28", "+28", "1_0", "٣", "9223372036854775808", "1" * 45])
def test_L1_a_limit_that_is_not_a_number(machine, value):
    """ASCII digits only, below 2**63."""
    machine.firmware_run(boot_lines("a"))
    res = machine.keeper(expect=1, env={"FWLK_KEEP_RUNS": value})

    assert len(res.lines) == 1 and res.starting("error: keeper: ")
    assert not machine.merged_root.exists()
    machine.check()


def test_L1_the_largest_limit(machine):
    run = machine.firmware_run(boot_lines("a"))
    machine.keeper(env={"FWLK_KEEP_RUNS": "9223372036854775807", "FWLK_KEEP_BYTES": "0009223372036854775807"})

    assert machine.log(run) == run.log()
    machine.check()


@pytest.mark.parametrize("count", ["9" * 40, "1_0", "١"])
def test_L1_memory_line_count_of_a_run_folder(machine, count):
    """sources.tsv damaged from outside: a count that is not a number stops that run, which keeps its folder."""
    boot = "ab" * 16
    folder = machine.merged_root / f"2026-11-21T100000_{boot}"
    folder.mkdir(parents=True)
    (folder / "sources.tsv").write_text(f"memory\t{'0' * 16}\t{count}\t-\t-\n")
    machine.remove_uefilogs()
    res = machine.keeper(expect=1, boot_id=machine.new_boot_id())

    errors = res.starting("error: ")
    assert len(errors) == 1 and errors[0].startswith("error: merge: ") and folder.name in errors[0]
    shutil.rmtree(folder)
    machine.check()


@pytest.mark.parametrize("field, value, kept", [
    (1, b"1.000", False),  # an uptime is a number
    (1, b"1" * 40 + b".000", True),  # but not one beyond 64 bits
    (1, b"--1.000", True),
    (1, b"1.5", True),
    (2, b"0\x1c", True),  # a segment with a separator control after it
    (0, b"2026-03-04T05:06:07.0", True),  # a time without its three digits of milliseconds
    (0, "2026-03-04T05:06:0٧.000".encode(), True),  # or with a digit that is not ASCII
])
def test_L1_numbers_of_the_unattached_log(machine, field, value, kept):
    """unattached/firmware.log damaged from outside while the firmware still holds the record of its row: if the log
    cannot be read, the record stays in the firmware."""
    machine.remove_uefilogs()
    rec = record(datetime(2026, 3, 4, 5, 6, 7))
    machine.add_record(rec)
    boot = machine.new_boot_id()
    machine.keeper(boot_id=boot)
    path = machine.merged_root / "unattached" / "firmware.log"
    log = path.read_bytes()
    rows = log.split(b"\n")
    fields = rows[1].split(b"\t")
    fields[field] = value
    rows[1] = b"\t".join(fields)
    path.write_bytes(b"\n".join(rows))
    name = machine.add_record(rec)
    res = machine.keeper(expect=1 if kept else 0, boot_id=boot)

    if kept:
        errors = res.starting("error: ")
        assert len(errors) == 2 and all(e.startswith("error: errrec: ") for e in errors) and name in errors[1]
        assert machine.records_in_firmware() == [name]
    else:
        assert res.starting("deleted ") == [f"deleted error record {name}"] and not res.starting("error: ")
        assert machine.records_in_firmware() == []
    path.write_bytes(log)
    machine.check()


def test_L1_lines_end_at_lf(machine):
    """A line of sources.tsv ends at LF only, and an empty line is skipped: a form feed is part of its line."""
    run = machine.firmware_run(boot_lines("a"))
    machine.keeper()
    path = machine.folder(run) / "sources.tsv"
    sources = path.read_bytes()
    path.write_bytes(b"\n" + sources.replace(b"\t-\t-\n", b"\t-\t\x0c\n\n"))
    machine.keeper()

    assert path.read_bytes() == sources and machine.log(run) == run.log()
    machine.check()


@pytest.mark.parametrize("value, ok", [(" \t{}\r\n", True), ("\x0b{}", False), ("{} ", False)])
def test_L1_white_space_is_space_tab_cr_lf(machine, value, ok):
    """Around the boot id, at the ends of a timing, and in a printed line."""
    run = machine.firmware_run(boot_lines("a"))
    (machine.fpdt / "z").write_text("12\x0b\n")
    fpdt = machine.root / "no\x0bsuch"
    res = machine.keeper(expect=0 if ok else 1, boot_id=value.format(run.boot_id),
                         env={"FWLK_SYSFS_ESRT": str(fpdt)})

    if ok:
        assert f"skipped esrt: nothing in {fpdt}\n" in res.out
        assert "z=12\x0b\n" in (machine.folder(run) / "fpdt-boot.txt").read_text()
    else:
        assert len(res.lines) == 1 and res.starting("error: keeper: ")
    (machine.fpdt / "z").unlink()
    machine.check()


@pytest.mark.parametrize("now", ["2026-11-21t05:00:00z", "2026-11-21T5:00:00Z", "2026-11-21T05:00:00Z ",
                                 "2026-11-21T05:00:60Z", "٢026-11-21T05:00:00Z"])
def test_L1_fwlk_now_has_one_format(machine, now):
    machine.firmware_run(boot_lines("a"))
    res = machine.keeper(expect=1, env={"FWLK_NOW": now})

    assert len(res.lines) == 1 and res.starting("error: keeper: ")
    machine.check()


def test_L1_utc_time_has_four_digits_of_year(machine):
    machine.firmware_run(boot_lines("a"))
    machine.keeper(env={"FWLK_NOW": "0999-01-02T03:04:05Z"})

    assert machine.bootefi_seen()[0][0] == "0999-01-02T030405Z"
    assert [line[0] for line in machine._tsv(machine.merged_root / "runs.tsv")] == ["0999-01-02T030405Z"]
    machine.check()


# ----- L2: the legacy keeper's files (migrate, Python reference only)

def test_L2_legacy_compressed_dump_that_is_cut(machine, tmp_path):
    machine.remove_uefilogs()
    boot, mem = machine.new_boot_id(), MemLog.from_lines(boot_lines("legacy"), T0)
    folder = legacy_folder(tmp_path / "legacy", "2026-11-20T030000Z", boot, mem, 1, keep=())
    (folder / "advlog.bin.xz").write_bytes(lzma.compress(mem.buffer())[:-20])
    res = machine.keeper("migrate", tmp_path / "legacy", boot_id=machine.new_boot_id(), expect=1)

    errors = res.starting("error: ")
    assert len(errors) == 1 and errors[0].startswith(f"error: memory: {folder}")
    assert folder.exists() and not machine.folder_names()
    machine.check()


def test_L2_legacy_text_with_seconds_beyond_any_date(machine, tmp_path):
    machine.remove_uefilogs()
    boot, mem = machine.new_boot_id(), MemLog.from_lines(boot_lines("legacy"), T0)
    folder = legacy_folder(tmp_path / "legacy", "2026-11-20T030000Z", boot, mem, 1, keep=())
    text = legacy_text(mem).split("\n")
    text.insert(2, "100000000000000.000 10:00:00.000 L80000000 far")
    (folder / "advlog.txt").write_text("\n".join(text))
    res = machine.keeper("migrate", tmp_path / "legacy", boot_id=machine.new_boot_id(), expect=1)

    errors = res.starting("error: ")
    assert len(errors) == 1 and errors[0].startswith(f"error: memory: {folder}")
    assert folder.exists()
    machine.check()


# ----- L3: names that are not UTF-8, and orders by bytes

def test_L3_staging_folders_in_the_order_of_their_bytes(machine):
    """A staging folder whose name is not UTF-8 is merged and removed; staging folders are taken in the order of
    their names' bytes (U+FF71 is the bytes EF BD B1, before the byte FF)."""
    crashed = [machine.firmware_run(boot_lines(f"crashed{i}"), reset=T0 + i * timedelta(hours=1)) for i in range(2)]
    for run, prefix in zip(crashed, (b"\xff", "ｱ".encode())):
        staging = machine.stage(run, datetime(2026, 11, 21, 4, 0, 0))
        staging.rename(staging.with_name(os.fsdecode(prefix + b"_" + run.boot_id.encode())))
    run = machine.firmware_run(boot_lines("now"), reset=T0 + 2 * timedelta(hours=1))
    machine.keeper()

    assert list(machine.memory_root.iterdir()) == []
    assert [line[2] for line in machine._tsv(machine.merged_root / "runs.tsv")] == [
        run.boot_id, crashed[1].boot_id, crashed[0].boot_id]
    assert [machine.log(r) for r in crashed + [run]] == [r.log() for r in crashed + [run]]
    machine.check()


def test_L3_legacy_record_copy_whose_name_is_not_utf8(machine, tmp_path):
    """Its name is printed with its bytes as they are, whatever the locale (here the one of the tests)."""
    machine.remove_uefilogs()
    boot, mem, rec = machine.new_boot_id(), MemLog.from_lines(boot_lines("legacy"), T0), record(T0 + 2 * S)
    legacy_folder(tmp_path / "legacy", "2026-11-20T030000Z", boot, mem, 1)
    legacy_records(tmp_path / "legacy", [])
    with open(os.fsencode(tmp_path / "legacy" / "hwerrrec") + b"/\xff.bin", "wb") as f:
        f.write(rec.raw)
    machine.memory_seen[boot] = mem
    res = machine.keeper("migrate", tmp_path / "legacy", boot_id=machine.new_boot_id())

    assert res.starting("archived error record ") == [f"archived error record {rec.hash} from �.bin"]
    assert (machine.merged_root / "hwerrrec" / f"{rec.hash}.bin").read_bytes() == rec.raw
    assert machine.log(boot) == log_bytes(merge(mem, None, [rec]))
    machine.check()


def test_L3_a_setting_that_is_not_utf8_is_refused(machine):
    machine.remove_uefilogs()
    root = machine.root / os.fsdecode(b"boot\xffefi")
    res = machine.keeper(expect=1, boot_id=machine.new_boot_id(), env={"FWLK_BOOTEFI_ROOT": str(root)})

    assert len(res.lines) == 1 and res.starting("error: keeper: ")
    assert not root.exists() and not machine.merged_root.exists() and not machine.memory_root.exists()
    machine.check()


def test_L3_a_timing_whose_name_is_not_utf8(machine):
    """It cannot be written in the text: the step fails, the other sources are kept."""
    run = machine.firmware_run(boot_lines("a"))
    odd = machine.fpdt / os.fsdecode(b"\xff")
    odd.write_text("1\n")
    res = machine.keeper(expect=1)

    errors = res.starting("error: ")
    assert len(errors) == 1 and errors[0].startswith("error: fpdt: ")
    assert not (machine.folder(run) / "fpdt-boot.txt").exists() and machine.log(run) == run.log()
    odd.unlink()
    machine.check()


def test_L3_a_run_folder_name_has_ascii_digits(machine):
    """A folder whose time label has other decimal digits is not a run folder: the keeper leaves it alone."""
    run = machine.firmware_run(boot_lines("a"))
    odd = machine.merged_root / f"٢٠٢٦-11-21T100000_{run.boot_id}"
    odd.mkdir(parents=True)
    (odd / "sources.tsv").write_text(f"memory\t{'0' * 16}\t1\t-\t-\n")
    machine.keeper()

    assert tree(odd) == {"sources.tsv": f"memory\t{'0' * 16}\t1\t-\t-\n".encode()}
    assert (machine.merged_root / run.folder_name / "firmware.log").read_bytes() == run.log()
    shutil.rmtree(odd)
    machine.check()


# ----- L4: removing a folder, reading a slot, comparing roots

@not_root
def test_L4_folder_removal_removes_files_before_subfolders(machine):
    crashed = machine.firmware_run(boot_lines("crashed"), reset=T0)
    staging = machine.stage(crashed, datetime(2026, 11, 21, 4, 0, 0))
    (staging / "sub").mkdir()
    (staging / "sub" / "f").write_bytes(b"x")
    (staging / "sub").chmod(0o555)
    machine.firmware_run(boot_lines("now"), reset=T0 + timedelta(hours=1))
    res = machine.keeper(expect=1)

    errors = res.starting("error: ")
    assert len(errors) == 1 and errors[0].startswith("error: purge: ")
    assert sorted(p.name for p in staging.rglob("*")) == ["f", "sub"]  # its files went before its subfolder
    (staging / "sub").chmod(0o755)
    machine.check()


def test_L4_slots_that_are_folders(machine):
    for slot in ("UEFI_Log1.txt", "UEFI_Log2.txt"):
        (machine.uefilogs / slot).mkdir()
    esp = tree(machine.uefilogs)
    res = machine.keeper(expect=1, boot_id=machine.new_boot_id())

    assert [line.split(": ")[1] for line in res.starting("error: ")] == ["UEFI_Log1.txt", "UEFI_Log2.txt"]
    assert tree(machine.uefilogs) == esp
    machine.check()


@pytest.mark.parametrize("bootefi", ["alias", "alias/inner"])
def test_L4_roots_compared_through_symbolic_links(machine, bootefi):
    """A link to the merged root, or a folder not made yet under it, is the merged root."""
    machine.remove_uefilogs()
    machine.merged_root.mkdir(parents=True)
    (machine.root / "alias").symlink_to("var/firmware")
    res = machine.keeper(expect=1, boot_id=machine.new_boot_id(), env={"FWLK_BOOTEFI_ROOT": str(machine.root / bootefi)})

    assert len(res.lines) == 1 and res.starting("error: keeper: ")
    assert list(machine.merged_root.iterdir()) == [] and not machine.memory_root.exists()
    machine.check()


# ----- L5: an output nobody reads

@pytest.mark.parametrize("args, rc", [((), 0), (("bogus",), 2)])
def test_L5_output_to_a_closed_pipe_is_dropped_and_the_run_goes_on(machine, args, rc):
    run = machine.firmware_run(boot_lines("a"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("FWLK_")}
    env.update(machine.env(run.boot_id, machine.now))
    machine.now += timedelta(minutes=1)
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        p = subprocess.run([*keeper_bin(), *args], stdout=write_end, stderr=subprocess.PIPE, env=env, timeout=600)
    finally:
        os.close(write_end)

    assert (p.returncode, p.stderr) == (rc, b"")
    if rc == 0:
        machine.memory_seen[run.boot_id] = run.mem
        assert machine.log(run) == run.log()
        machine.keeper()  # nothing left to do
        assert machine.log(run) == run.log()
    machine.check()


# ----- L6: retention

def test_L6_retention_stops_at_a_file_it_cannot_size(machine):
    """The keeper is run here without the parity copy, which cannot copy links to nothing."""
    run = machine.firmware_run(boot_lines("a"))
    machine.keeper()
    folder = machine.folder(run)
    (folder / "top").symlink_to("gone")
    env = {k: v for k, v in os.environ.items() if not k.startswith("FWLK_")}
    env.update(machine.env(run.boot_id, machine.now))
    machine.now += timedelta(minutes=1)
    p = subprocess.run(keeper_bin(), env=env, capture_output=True, timeout=600)

    assert (p.returncode, p.stderr) == (1, b"")
    assert [line for line in p.stdout.decode().splitlines() if line.startswith("error: ")][0].startswith("error: retention: ")
    (folder / "top").unlink()
    machine.check()


def test_L6_retention_adds_sizes_beyond_64_bits(machine):
    """Four sparse files of 2**62 bytes make the oldest folder larger than any 64-bit sum: it goes. The keeper is
    run here without the parity copy, which would read those files."""
    runs = []
    for i in range(3):
        runs.append(machine.firmware_run(boot_lines(f"b{i}"), reset=T0 + i * timedelta(hours=1)))
        machine.keeper()
    oldest = machine.folder(runs[0])
    try:
        for k in range(4):
            with open(oldest / f"sparse{k}", "wb") as f:
                f.truncate(2**62)
    except OSError as e:
        pytest.skip(f"no sparse file of 2**62 bytes here: {e}")
    machine.remove_uefilogs()
    run = machine.firmware_run(boot_lines("b3"), reset=T0 + 3 * timedelta(hours=1))
    env = {k: v for k, v in os.environ.items() if not k.startswith("FWLK_")}
    env.update(machine.env(run.boot_id, machine.now))
    machine.now += timedelta(minutes=1)
    p = subprocess.run(keeper_bin(), env=env, capture_output=True, timeout=600)

    assert (p.returncode, p.stderr) == (0, b"")
    assert not oldest.exists()
    machine.memory_seen[run.boot_id] = run.mem
    assert [machine.log(r) for r in runs[1:] + [run]] == [r.log() for r in runs[1:] + [run]]
    machine.check()
