"""The 'machine' fixture: a laptop on scratch directories, driven boot by boot, and the standing rules H1 to H7.

`migrate` is a command of the Python reference only: a test that calls it is skipped when FWLK_BIN or FWLK_PARITY_BIN
names another keeper. The real-data test runs its migrate step with the reference launcher (keeper(reference=True)).

The keeper is run only as a program: the executable named by FWLK_BIN (default: the Python reference launcher),
configured through the FWLK_* variables of SPEC.md section 6, judged on the files it reads and writes.
Parity mode (H7): when FWLK_PARITY_BIN names a second keeper, every keeper call is also made by it on a copy of the
test's scratch tree taken just before the call, and both must give the same exit status, output and files.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from builders import (HWERR_GUID, LOCATOR_VAR, PAD_BYTES, SUPPORT_VAR, FileLog, MemLog, Record, Row, esrt_text,
                      fpdt_text, log_bytes, merge, norm, parse_content, parse_log, record_var, sha16, sort_key,
                      unattached_key, untag, uptime_ms, utc_stamp, write_sysfs)

DEFAULT_BIN = Path(__file__).resolve().parents[1] / "firmware-log-keeper"

KEY = r"[0-9a-f]{32}|fileonly-[0-9a-f]{16}"
FOLDER_RE = re.compile(rf"([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{6}})_({KEY})")
RECORD_VAR_RE = re.compile(rf"HwErrRec[0-9A-Fa-f]{{4}}-{HWERR_GUID}")
MERGED_RE = re.compile(rf"merged run (?P<key>{KEY}) into (?P<folder>\S+) "
                       r"\(memory (?P<mem>\d+) lines, bootefi (?P<file>\d+) lines, errrec (?P<rec>\d+)\)")
# SPEC section 5: the only lines the keeper may print (H6)
OUTPUT_SHAPES = [MERGED_RE] + [re.compile(p) for p in (
    r"archived file log [0-9a-f]{16} from UEFI_Log[1-9]\.txt",
    r"archived error record [0-9a-f]{16} from \S+",
    r"deleted error record \S+",
    r"skipped [^:]+: .+",
    r"error: [^:]+: .+")]
STAMP = r"\d{4}-\d\d-\d\dT\d{6}Z"
BOOTEFI_SEEN_RE = re.compile(rf"{STAMP}\t[0-9a-f]{{32}}\tUEFI_Log[1-9]\.txt\t[0-9a-f]{{16}}\t\d+\t\d+\t(yes|no)\t[^\t]+\t[^\t]+")
RUNS_RE = re.compile(rf"{STAMP}\t[0-9a-f]{{32}}\t({KEY})")
ERRREC_SEEN_RE = re.compile(rf"{STAMP}\t[0-9a-f]{{32}}\t(archived|deleted|kept)\t[^\t]+\t[0-9a-f]{{16}}\t"
                            r"(-|archive-mismatch|not-verified|delete-failed)")
SOURCE_ORDER = {"memory": 0, "bootefi": 1, "errrec": 2}

not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def keeper_bin() -> list[str]:
    """The command that runs the keeper: the executable named by FWLK_BIN, else the Python reference launcher
    under the interpreter that runs the tests (its own first line names the system's Python)."""
    exe = Path(os.environ.get("FWLK_BIN") or DEFAULT_BIN)
    if not exe.is_file():
        pytest.fail(f"keeper executable not found: {exe} (set FWLK_BIN)", pytrace=False)
    path = str(exe.resolve())  # a bare name would be looked up in PATH, where another program may have it
    return [path] if os.environ.get("FWLK_BIN") else [sys.executable, "-I", path]


def is_reference(name: str | None) -> bool:
    """Does this variable name the Python reference launcher (unset: the default)?"""
    return not name or Path(name).resolve() == DEFAULT_BIN.resolve()


def same_output(first: bytes, second: bytes) -> bool:
    """H7: line for line, but in an error line only `error: <step>:` is fixed, and in a usage line only `usage:`."""
    fixed = lambda line: (re.match(rb"error: [^:]*:|usage:", line) or [line])[0]
    a, b = first.split(b"\n"), second.split(b"\n")
    return len(a) == len(b) and all(fixed(x) == fixed(y) for x, y in zip(a, b))


def digest(root: Path):
    """Hash of a whole tree (names and bytes); None when it does not exist."""
    if not root.exists():
        return None
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode() + b"\0")
        if p.is_file():
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def files_of(root: Path) -> dict[str, bytes | None]:
    """Every entry of a tree but the linked /dev/mem files: relative name -> bytes (None for a folder)."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.name != "devmem":
            out[str(p.relative_to(root))] = None if p.is_dir() else p.read_bytes()
    return out


@dataclass
class Result:
    rc: int
    out: str
    err: str
    now: datetime  # the time given to the keeper (FWLK_NOW)
    boot_id: str

    @property
    def utc(self) -> str:
        return utc_stamp(self.now)

    @property
    def lines(self) -> list[str]:
        return self.out.split("\n")[:-1]  # a line ends at LF only (SPEC 5)

    def starting(self, prefix: str) -> list[str]:
        return [line for line in self.lines if line.startswith(prefix)]

    def merged(self, key: str) -> tuple:
        """(folder name, memory lines, file lines, records) of the one 'merged run' line of this run key."""
        found = [m for m in map(MERGED_RE.fullmatch, self.lines) if m and m["key"] == key]
        assert len(found) == 1, f"expected one 'merged run {key}' line in:\n{self.out}"
        m = found[0]
        return m["folder"].rstrip("/").rsplit("/", 1)[-1], int(m["mem"]), int(m["file"]), int(m["rec"])

    def __str__(self) -> str:
        return f"exit status {self.rc}\n--- stdout\n{self.out}--- stderr\n{self.err}"


@dataclass
class Run:
    """One firmware run of the story: what it logged in memory, what it wrote to the EFI partition."""
    n: int
    reset: datetime
    mem: MemLog | None
    file: FileLog | None
    boot_id: str | None  # None for a run that never reached Linux
    slot: int | None

    @property
    def key(self) -> str:
        return self.boot_id or f"fileonly-{self.file.hash}"

    def rows(self, records=()) -> list[Row]:
        return merge(self.mem, self.file, records)

    def log(self, records=()) -> bytes:
        """The expected firmware.log of this run when the keeper has seen all its sources."""
        return log_bytes(self.rows(records))

    @property
    def folder_name(self) -> str:
        return f"{self.rows()[0].time[:19].replace(':', '')}_{self.key}"


class Machine:
    """A laptop on scratch directories. The test plays the firmware, then runs the keeper as Linux would."""

    def __init__(self, root: Path):
        self.root = root
        self.memory_root = root / "var" / "firmware_memory"
        self.bootefi_root = root / "var" / "firmware_bootefilogs"
        self.merged_root = root / "var" / "firmware"
        self.efivars = root / "efivars"
        self.esp = root / "esp"
        self.uefilogs = self.esp / "UefiLogs"
        self.devmem = root / "devmem"
        self.fpdt = root / "sys" / "fpdt"
        self.esrt = root / "sys" / "esrt"
        self.efivars.mkdir(parents=True)
        self.uefilogs.mkdir(parents=True)
        (self.efivars / SUPPORT_VAR).write_bytes(b"\x07\x00\x00\x00\x01\x00")
        self.now = datetime(2026, 11, 21, 5, 0, 0)  # UTC time of the next keeper run, one minute more each time
        self.clock = datetime(2026, 11, 20, 12, 0, 0)  # firmware clock at the next reset, one hour more each time
        self.runs: list[Run] = []
        self.current: Run | None = None
        self.index = 0  # last slot the firmware wrote
        self.boots = 0
        self.memory_ok = True
        self.memory_seen: dict[str, MemLog] = {}  # boot id -> memory log a keeper run was given
        self.results: list[Result] = []
        self.violations: list[str] = []
        self.checked = False

    # ----- the firmware's side ---------------------------------------------------------------------------------

    def new_boot_id(self) -> str:
        self.boots += 1
        return hashlib.md5(b"boot %d" % self.boots).hexdigest()

    def firmware_run(self, lines=None, *, reset: datetime | None = None, late=(), linux: bool = True,
                     memlog: MemLog | None = None, file_lines=None, filelog: FileLog | None = None,
                     write_file: bool = True, complete: bool = True, pad: str = "lines",
                     phase: str | None = None) -> Run:
        """One power-on. The firmware logs `lines` in memory, flushes them to the next UEFI_Log slot at the
        hand-over (if the UefiLogs folder exists), then logs `late` in memory only. With linux=False the machine
        hangs after the hand-over: the memory log is lost. file_lines: what the file holds when it differs.
        phase: the firmware tags the lines of its file log (see FileLog).
        """
        reset = reset or self.clock
        self.clock = reset + timedelta(hours=1)
        lines = list(lines or [])
        mem = None
        if linux:
            mem = memlog or MemLog.from_lines(lines + list(late), reset)
        file = slot = None
        if write_file and self.uefilogs.is_dir():
            if filelog is None:
                written = list(lines if file_lines is None else file_lines)
                last = max((x.ms for x in written if not isinstance(x, bytes)), default=0)
                end = (reset + timedelta(milliseconds=last)).replace(microsecond=0) if complete else None
                filelog = FileLog(written, end, pad=pad, phase=phase)
            file, slot = filelog, self.write_file(filelog)
        run = Run(len(self.runs) + 1, reset, mem, file, self.new_boot_id() if linux else None, slot)
        self.runs.append(run)
        self.current = run
        self.memory_ok = linux
        self.devmem.unlink(missing_ok=True)
        (self.efivars / LOCATOR_VAR).unlink(missing_ok=True)
        if linux:
            mem.write(self.devmem, self.efivars)
            write_sysfs(self.fpdt, self.esrt, run.n)
        return run

    def write_file(self, filelog: FileLog | bytes) -> int:
        """The firmware writes a file log: next slot of the rotation (1 to 9), index file updated."""
        self.index = self.index % 9 + 1
        data = filelog if isinstance(filelog, bytes) else filelog.file_bytes()
        (self.uefilogs / f"UEFI_Log{self.index}.txt").write_bytes(data)
        (self.uefilogs / "UEFI_Index.txt").write_text(f"{self.index}\n")
        return self.index

    def remove_uefilogs(self) -> None:
        shutil.rmtree(self.uefilogs)

    def add_record(self, rec: Record, slot: int | None = None, guid: str = HWERR_GUID) -> str:
        """The firmware writes an error record in its first free slot. Returns the variable's file name."""
        if slot is None:
            slot = next(i for i in range(0x10000) if not (self.efivars / record_var(i, guid)).exists())
        name = record_var(slot, guid)
        (self.efivars / name).write_bytes(rec.raw)
        return name

    def records_in_firmware(self) -> list[str]:
        return sorted(p.name for p in self.efivars.iterdir() if RECORD_VAR_RE.fullmatch(p.name))

    def corrupt_memory(self) -> None:
        """The locator variable points at something that is not an Advanced Logger buffer."""
        with open(self.devmem, "r+b") as f:
            f.seek(self.current.mem.addr)
            f.write(b"\xde\xad\xbe\xef" * 64)
        self.memory_ok = False

    def stage(self, run: Run, when: datetime) -> Path:
        """The staging folder a keeper run of that boot would have left had it crashed before merging."""
        folder = self.memory_root / f"{utc_stamp(when)}_{run.boot_id}"
        folder.mkdir(parents=True)
        (folder / "advlog.bin").write_bytes(run.mem.buffer())
        (folder / "fpdt-boot.txt").write_text(fpdt_text(run.n))
        (folder / "esrt.txt").write_text(esrt_text())
        self.memory_seen[run.boot_id] = run.mem
        return folder

    # ----- Linux's side: one keeper run ------------------------------------------------------------------------

    def env(self, boot_id: str, now: datetime) -> dict[str, str]:
        return {"FWLK_MEMORY_ROOT": str(self.memory_root), "FWLK_BOOTEFI_ROOT": str(self.bootefi_root),
                "FWLK_MERGED_ROOT": str(self.merged_root), "FWLK_EFIVARS": str(self.efivars),
                "FWLK_UEFILOGS": str(self.uefilogs), "FWLK_DEVMEM": str(self.devmem),
                "FWLK_SYSFS_FPDT": str(self.fpdt), "FWLK_SYSFS_ESRT": str(self.esrt),
                "FWLK_BOOT_ID": boot_id, "FWLK_NOW": now.strftime("%Y-%m-%dT%H:%M:%SZ")}

    def _vars(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in self.efivars.iterdir()} if self.efivars.is_dir() else {}

    def _esp_files(self) -> list[Path]:
        return sorted(p for p in self.esp.rglob("*") if p.is_file()) if self.esp.is_dir() else []

    def keeper(self, *args, expect: int = 0, env: dict | None = None, boot_id: str | None = None,
               cwd: Path | None = None, reference: bool = False) -> Result:
        """Run the keeper once in the current Linux boot. Every path variable points into the scratch directory.

        env: more variables for the keeper; a value of None removes the variable. reference: run the Python
        reference launcher, without parity check, whatever keeper is under test."""
        if (args and args[0] == "migrate" and expect != 2 and not reference
                and not (is_reference(os.environ.get("FWLK_BIN")) and is_reference(os.environ.get("FWLK_PARITY_BIN")))):
            pytest.skip("migrate is a command of the Python reference only, and FWLK_BIN or FWLK_PARITY_BIN names "
                        "another keeper")
        exe = [sys.executable, "-I", str(DEFAULT_BIN)] if reference else keeper_bin()
        boot_id = boot_id or (self.current and self.current.boot_id)
        assert boot_id, "the keeper only runs in a boot that reached Linux"
        now, self.now = self.now, self.now + timedelta(minutes=1)
        full = {k: v for k, v in os.environ.items() if not k.startswith("FWLK_")}
        full.update(self.env(boot_id, now))
        full.update(env or {})
        full = {k: v for k, v in full.items() if v is not None}
        twin = None if reference else self._parity_copy()  # before the access times are set: copying reads the EFI partition
        esp_before, vars_before = digest(self.esp), self._vars()
        for p in self._esp_files():  # an access time older than the change time: any plain read would update it
            os.utime(p, ns=(p.stat().st_mtime_ns - 3600 * 10**9, p.stat().st_mtime_ns))
        accessed = [p.stat().st_atime_ns for p in self._esp_files()]
        mem = self.current.mem if self.current and self.current.boot_id == boot_id else None
        if (not args and mem and self.memory_ok and (self.efivars / LOCATOR_VAR).exists()
                and os.access(self.devmem, os.R_OK)):
            self.memory_seen[boot_id] = mem
        p = subprocess.run([*exe, *map(str, args)], env=full, cwd=cwd, capture_output=True, timeout=600)
        result = Result(p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"), now, boot_id)
        self.results.append(result)
        if [p.stat().st_atime_ns for p in self._esp_files()] != accessed:
            self.violations.append("H1: a file of the EFI partition was read with its access time updated")
        if digest(self.esp) != esp_before:
            self.violations.append("H1: the EFI partition changed during a keeper run")
        vars_after = self._vars()
        for name, data in vars_before.items():
            if name not in vars_after:
                copy = self.merged_root / "hwerrrec" / f"{sha16(data)}.bin"
                if not (RECORD_VAR_RE.fullmatch(name) and copy.is_file() and copy.read_bytes() == data):
                    self.violations.append(f"H2: {name} was deleted without being an archived error record")
            elif vars_after[name] != data:
                self.violations.append(f"H2: {name} was modified")
        self.violations += [f"H2: {name} was created" for name in vars_after.keys() - vars_before.keys()]
        if twin:  # after the checks of H1: comparing reads the EFI partition
            self._parity_check(twin, p, [*map(str, args)], full, cwd)
        assert result.rc == expect, f"expected exit status {expect}\n{result}"
        return result

    # ----- H7: parity with a second keeper --------------------------------------------------------------------

    def _parity_copy(self) -> Path | None:
        """Copy the test's scratch tree (the machine and what the test put beside it, such as a legacy root) next to
        it, for the second keeper. The fake /dev/mem files are linked, not copied: they are large and only read."""
        if not os.environ.get("FWLK_PARITY_BIN"):
            return None
        base = self.root.parent
        twin = base.with_name(base.name + "-parity")
        if twin.exists():
            writable(twin)
            shutil.rmtree(twin)
        shutil.copytree(base, twin, symlinks=True, ignore=lambda d, names: [n for n in names if n == "devmem"])
        for p in base.rglob("devmem"):
            (twin / p.relative_to(base)).symlink_to(p)
        return twin

    def _parity_check(self, twin: Path, first, args: list[str], env: dict, cwd: Path | None) -> None:
        """Run the second keeper on the copy and compare it with the first keeper's run on the tree itself."""
        base = str(self.root.parent)
        move = lambda v: v.replace(base, str(twin))
        p = subprocess.run([str(Path(os.environ["FWLK_PARITY_BIN"]).resolve()), *map(move, args)],
                           env={k: move(v) for k, v in env.items()}, cwd=cwd and move(str(cwd)),
                           capture_output=True, timeout=600)
        diffs = []
        if p.returncode != first.returncode:
            diffs.append(f"exit status {first.returncode} != {p.returncode}")
        if not same_output(first.stdout, p.stdout.replace(str(twin).encode(), base.encode())):
            diffs.append(f"standard output\n--- {keeper_bin()[-1]}\n{first.stdout.decode('utf-8', 'replace')}"
                         f"--- {os.environ['FWLK_PARITY_BIN']}\n{p.stdout.decode('utf-8', 'replace')}")
        if p.stderr != first.stderr:
            diffs.append(f"standard error {first.stderr!r} != {p.stderr!r}")
        mine, theirs = files_of(Path(base)), files_of(twin)
        for name in sorted(mine.keys() | theirs.keys()):
            if name not in mine or name not in theirs:
                diffs.append(f"file {name}: only in the {'first' if name in mine else 'second'} tree")
            elif mine[name] != (theirs[name] and theirs[name].replace(str(twin).encode(), base.encode())):
                diffs.append(f"file {name}: different bytes")
        writable(twin)
        shutil.rmtree(twin)
        assert not diffs, "H7: the second keeper (FWLK_PARITY_BIN) differs:\n" + "\n".join(diffs)

    # ----- reading the archive ---------------------------------------------------------------------------------

    def folder_names(self) -> list[str]:
        """Names of the run folders, sorted."""
        if not self.merged_root.is_dir():
            return []
        return sorted(p.name for p in self.merged_root.iterdir() if p.name not in ("hwerrrec", "unattached", "runs.tsv"))

    def folder(self, run_or_key) -> Path:
        key = run_or_key if isinstance(run_or_key, str) else run_or_key.key
        found = [name for name in self.folder_names() if name.endswith("_" + key)]
        assert len(found) == 1, f"expected one folder for run {key}, found {found} in {self.folder_names()}"
        return self.merged_root / found[0]

    def log(self, run_or_key) -> bytes:
        return (self.folder(run_or_key) / "firmware.log").read_bytes()

    def rows(self, run_or_key) -> list[Row]:
        return parse_log(self.log(run_or_key))

    def sources(self, run_or_key) -> str:
        return (self.folder(run_or_key) / "sources.tsv").read_text()

    def unattached(self) -> bytes:
        return (self.merged_root / "unattached" / "firmware.log").read_bytes()

    @staticmethod
    def _tsv(path: Path) -> list[tuple]:
        return [tuple(line.split("\t")) for line in path.read_text().splitlines()] if path.is_file() else []

    def bootefi_seen(self) -> list[tuple]:
        return self._tsv(self.bootefi_root / "seen.tsv")

    def errrec_seen(self) -> list[tuple]:
        return self._tsv(self.merged_root / "hwerrrec" / "seen.tsv")

    def raw_logs(self) -> set[str]:
        """Hashes of the archived raw file logs."""
        return {p.stem for p in self.bootefi_root.glob("*.txt")} if self.bootefi_root.is_dir() else set()

    def record_copies(self) -> set[str]:
        return {p.stem for p in (self.merged_root / "hwerrrec").glob("*.bin")}

    def seen_line(self, result: Result, file: FileLog, slot: int, index: int | str) -> tuple:
        """The firmware_bootefilogs/seen.tsv line of one file at one keeper run."""
        return (result.utc, result.boot_id, f"UEFI_Log{slot}.txt", file.hash, str(len(file.content)),
                str(file.trailing), "yes" if file.complete else "no", file.end_as_written or "-", str(index))

    # ----- standing rules --------------------------------------------------------------------------------------

    def check(self, bad_copies=()) -> None:
        """Rules H1 to H6 of TEST-SCENARIOS.md (H7 is checked at every keeper call, in parity mode), asserted at the end of every scenario.

        bad_copies: hashes of error-record archive files a test damaged on purpose.
        """
        self.checked = True
        assert not self.violations, "\n".join(self.violations)  # H1, H2 (checked around every keeper run)
        for result in self.results:  # H6
            if result.rc == 2:
                continue  # usage error: not a run
            for line in result.lines:
                assert any(shape.fullmatch(line) for shape in OUTPUT_SHAPES), f"H6: unexpected output line {line!r}"
            assert result.err == "", f"H6: the keeper wrote to standard error:\n{result.err}"
        self._check_archive(set(bad_copies))  # H3, H4
        if self.results and self.results[-1].rc == 0 and self.memory_root.is_dir():  # H5
            assert not list(self.memory_root.iterdir()), "H5: staging data left after a successful run"

    def _check_archive(self, bad_copies: set[str]) -> None:
        raw = {}
        if self.bootefi_root.is_dir():
            for p in self.bootefi_root.iterdir():
                if p.name == "seen.tsv":
                    for line in p.read_text().splitlines():
                        assert BOOTEFI_SEEN_RE.fullmatch(line), f"firmware_bootefilogs/seen.tsv line {line!r}"
                    continue
                assert re.fullmatch(r"[0-9a-f]{16}\.txt", p.name), f"H4: unexpected {p}"
                data = p.read_bytes()
                assert sha16(data) == p.stem, f"H4: {p.name} is not named after its content"
                assert data and data == data.rstrip(PAD_BYTES), f"{p.name} holds padding"
                raw[p.stem] = data
        referenced, keys, record_rows, made = set(), [], [], []
        if self.merged_root.is_dir():
            for p in sorted(self.merged_root.iterdir()):
                if p.name == "runs.tsv":
                    for line in p.read_text().splitlines():
                        assert RUNS_RE.fullmatch(line), f"runs.tsv line {line!r}"
                        made.append(line.split("\t")[2])
                elif p.name == "hwerrrec":
                    self._check_record_archive(p, bad_copies)
                elif p.name == "unattached":
                    assert [f.name for f in p.iterdir()] in ([], ["firmware.log"]), f"unexpected files in {p}"
                    rows = parse_log((p / "firmware.log").read_bytes()) if list(p.iterdir()) else []  # none passed its check yet
                    assert all(r.source == "errrec" and r.uptime == "-" for r in rows)
                    assert rows == sorted(rows, key=unattached_key), "unattached/firmware.log is not in order"
                    record_rows += rows
                else:
                    m = FOLDER_RE.fullmatch(p.name)
                    assert m and p.is_dir(), f"H4: unexpected {p}"
                    keys.append(m[2])
                    record_rows += self._check_run_folder(p, m[1], m[2], raw, referenced)
        assert len(keys) == len(set(keys)), f"H4: two folders for one run: {sorted(keys)}"
        assert set(keys) <= set(made), f"run folders that runs.tsv does not list: {sorted(set(keys) - set(made))}"
        assert set(raw) <= referenced, f"H4: raw file logs in no run folder: {sorted(set(raw) - referenced)}"
        hashes = [re.search(r"hash=([0-9a-f]{16})", r.message)[1] for r in record_rows]
        assert len(hashes) == len(set(hashes)), "H4: an error record has more than one row"

    def _check_record_archive(self, folder: Path, bad_copies: set[str]) -> None:
        for p in folder.iterdir():
            if p.name == "seen.tsv":
                for line in p.read_text().splitlines():
                    assert ERRREC_SEEN_RE.fullmatch(line), f"hwerrrec/seen.tsv line {line!r}"
            elif p.name != "legacy-index.txt":
                assert re.fullmatch(r"[0-9a-f]{16}\.bin", p.name), f"H4: unexpected {p}"
                assert p.stem in bad_copies or sha16(p.read_bytes()) == p.stem, f"H4: {p.name} is not named after its content"

    def _check_run_folder(self, folder: Path, time_label: str, key: str, raw: dict, referenced: set) -> list[Row]:
        """One run folder against its sources (H3) and against itself (H4). Returns its error-record rows."""
        names = {f.name for f in folder.iterdir()}
        for name in [n for n in names if re.fullmatch(r"advlog-[0-9a-f]{16}\.bin", n)]:  # a dump not fully decoded
            assert sha16((folder / name).read_bytes()) == name[7:23], f"H4: {name} is not named after its content"
            assert not key.startswith("fileonly-"), f"{folder.name} has a memory dump"
            names.remove(name)
        assert {"firmware.log", "sources.tsv"} <= names <= {"firmware.log", "sources.tsv", "fpdt-boot.txt", "esrt.txt"}, \
            f"files of {folder.name}: {sorted(names)}"
        rows = parse_log((folder / "firmware.log").read_bytes())
        order = [sort_key(r) for r in rows]
        assert all(a < b for a, b in zip(order, order[1:])), f"{folder.name}/firmware.log is not in the order of SPEC 3.4"
        if rows and rows[0].time != "-":
            assert time_label == rows[0].time[:19].replace(":", ""), f"{folder.name}: label is not the first row's time"
        sources = self._tsv(folder / "sources.tsv")
        for s in sources:
            assert len(s) == 5 and s[0] in SOURCE_ORDER and re.fullmatch(r"[0-9a-f]{16}", s[1]) and s[2].isdigit(), s
            if s[0] == "bootefi":
                assert re.fullmatch(r"UEFI_Log[1-9]\.txt", s[3]) and s[4] in ("merged", "extra"), s
            else:
                assert s[3:] == ("-", "-"), s
        assert sources == sorted(sources, key=lambda s: (SOURCE_ORDER[s[0]], s[4] != "merged", s[1])), \
            f"{folder.name}/sources.tsv is not in order"
        memory = [s for s in sources if s[0] == "memory"]
        files = [s for s in sources if s[0] == "bootefi"]
        merged = [s for s in files if s[4] == "merged"]
        assert len(memory) <= 1 and len(merged) <= 1, sources
        # H3, memory log: every line number once
        mem_rows = sorted((r for r in rows if r.mem_line != "-"), key=lambda r: int(r.mem_line))
        count = int(memory[0][2]) if memory else 0
        assert [int(r.mem_line) for r in mem_rows] == list(range(1, count + 1)), f"H3: memory lines of {folder.name}"
        if key in self.memory_seen:
            expected = [(x.ms, norm(x.text), f"L{x.level:08x}") for x in self.memory_seen[key].lines()]
            assert [(uptime_ms(r.uptime), r.message, r.level) for r in mem_rows] == expected, \
                f"H3: memory lines of {folder.name} differ from the memory log"
        # H3, file log: every line of the raw copy once, at its line number
        file_rows = sorted((int(r.file_line), uptime_ms(r.uptime), r.message, r.source) for r in rows if r.file_line != "-")
        if merged:
            assert merged[0][1] in raw, f"raw copy {merged[0][1]} of {folder.name} is missing"
            expected = [(x.n, x.ms, norm(x.text)) for x in parse_content(raw[merged[0][1]])]
            # a row paired with the memory log holds the memory copy's text: the file's without the firmware's tags
            assert len(file_rows) == len(expected) and all(
                (n, ms) == (x[0], x[1]) and (text == x[2] or source == "both" and text == untag(x[2]))
                for (n, ms, text, source), x in zip(file_rows, expected)), \
                f"H3: file lines of {folder.name} differ from the raw copy"
            assert int(merged[0][2]) == len(expected), sources
        else:
            assert not file_rows, f"{folder.name} has file rows and no merged file log"
        # H3, sources of the rows: a file log merged with a memory log is all in it (SPEC 3.1)
        assert {r.source for r in rows} <= ({"both", "memory", "errrec"} if memory else {"bootefi", "errrec"}), \
            f"H3: {folder.name} has rows of a source it should not have"
        for s in files:
            assert s[1] in raw, f"raw copy {s[1]} listed in {folder.name} is missing"
            assert s[1] not in referenced, f"H4: file log {s[1]} is in two run folders"
            referenced.add(s[1])
        # error records: one row per listed record, each with its archive copy
        record_rows = [r for r in rows if r.source == "errrec"]
        hashes = sorted(re.search(r"hash=([0-9a-f]{16})", r.message)[1] for r in record_rows)
        assert hashes == sorted(s[1] for s in sources if s[0] == "errrec"), f"error records of {folder.name}"
        for h in hashes:
            assert (self.merged_root / "hwerrrec" / f"{h}.bin").is_file(), f"no archive copy of record {h}"
        if key.startswith("fileonly-"):
            assert not memory and merged and merged[0][1] == key[len("fileonly-"):], f"{folder.name}: {sources}"
            assert names == {"firmware.log", "sources.tsv"}, f"{folder.name} never reached Linux: {sorted(names)}"
        return record_rows

    def cleanup(self) -> None:
        """Give permissions back and drop the large fake sources."""
        for path, dirs, files in os.walk(self.root):
            os.chmod(path, 0o755)
            for name in files:
                os.chmod(os.path.join(path, name), 0o644)
        shutil.rmtree(self.esp, ignore_errors=True)
        self.devmem.unlink(missing_ok=True)


def read_only(root: Path) -> None:
    """Make a tree read-only for the keeper (a read-only /var/log, a full disk)."""
    for path, dirs, files in os.walk(root):
        for name in files:
            os.chmod(os.path.join(path, name), 0o444)
        os.chmod(path, 0o555)


def writable(root: Path) -> None:
    for path, dirs, files in os.walk(root):
        os.chmod(path, 0o755)
        for name in files:
            os.chmod(os.path.join(path, name), 0o644)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if call.when == "call":
        item.call_passed = outcome.get_result().passed


@pytest.fixture
def machines(tmp_path, request):
    """Factory of machines, for scenarios that compare two of them."""
    made = []

    def make() -> Machine:
        made.append(Machine(tmp_path / f"machine{len(made)}"))
        return made[-1]

    yield make
    for m in made:
        m.cleanup()
    if getattr(request.node, "call_passed", False):
        for m in made:
            assert m.checked or not m.results, "the scenario did not assert the standing rules: call machine.check()"


@pytest.fixture
def machine(machines) -> Machine:
    return machines()
