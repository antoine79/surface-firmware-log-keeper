"""The archive of SPEC section 2: atomic writes, the raw copies, the run folders, the seen.tsv files, retention."""
import errno
import hashlib
import os
import re
import stat

from . import cper, merge, purge
from .filelog import FileLog
from .merge import number

FILE_ONLY = "fileonly-"  # key prefix of a run known by its file log only
FOLDER = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6})_([0-9a-f]{32}|fileonly-[0-9a-f]{16})")
REMOVING = ".removing"  # end of the name a run folder takes while it is being removed


def sha16(data):
    return hashlib.sha256(data).hexdigest()[:16]


def lines(data):
    """The lines of a text file, each ended by LF; empty lines are skipped (SPEC 2)."""
    return [line for line in data.decode("utf-8").split("\n") if line]


def read(path):
    with open(path, "rb") as f:
        return f.read()


def read_or_none(path):
    try:
        return read(path)
    except FileNotFoundError:
        return None


def read_untouched(path):
    """Read a file of the EFI partition without updating its access time: that update is a write to the partition.
    None if the file does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOATIME)
    except FileNotFoundError:
        return None
    except PermissionError:  # O_NOATIME is for the file's owner and for root only
        fd = os.open(path, os.O_RDONLY)
    if stat.S_ISDIR(os.fstat(fd).st_mode):  # open(fd) would name the descriptor, and leave it open
        os.close(fd)
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), path)
    with open(fd, "rb") as f:
        return f.read()


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_dirs(path):
    """Create a folder and flush every folder entry made on the way: a purge may depend on it."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        make_dirs(os.path.dirname(path))
        os.makedirs(path, exist_ok=True)
        fsync_dir(os.path.dirname(path))


def write(path, data, check=None):
    """Write an archive file: temporary file, fsync, read back and compare, then rename.

    check: called with the bytes read back; if it raises, the file that was there stays as it was."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        back = read(tmp)
        if back != data:
            raise OSError(f"{path} differs from what was written")
        if check:
            check(back)
        os.rename(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    fsync_dir(os.path.dirname(path))


def append(path, fields):
    """One line more in an append-only .tsv file."""
    make_dirs(os.path.dirname(path))
    with open(path, "ab") as f:
        f.write(("\t".join(fields) + "\n").encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root, self.bootefi = cfg.merged_root, cfg.bootefi_root
        self.records_dir = os.path.join(self.root, "hwerrrec")
        self.unattached_log = os.path.join(self.root, "unattached", "firmware.log")
        self.runs_tsv = os.path.join(self.root, "runs.tsv")
        self._files, self._records = {}, {}
        self.folders = {}  # run key -> folder name
        self.file_owners, self.record_owners = {}, {}  # content hash -> keys of the runs listing it
        self.unfinished = set()  # keys of the run folders without a log: its write was stopped, or failed its check
        for name in self.folder_names():
            key = FOLDER.fullmatch(name)[2]
            self.folders[key] = name
            if not os.path.isfile(os.path.join(self.root, name, "firmware.log")):
                self.unfinished.add(key)
            for kind, h in self.listed(name):
                {"bootefi": self.file_owners, "errrec": self.record_owners}.get(kind, {}).setdefault(h, set()).add(key)

    def folder_names(self):
        try:
            names = os.listdir(self.root)
        except FileNotFoundError:
            return []
        return sorted(n for n in names if FOLDER.fullmatch(n) and os.path.isdir(os.path.join(self.root, n)))

    def listed(self, name):
        """(kind, hash) of every line of a run folder's sources.tsv."""
        data = read_or_none(os.path.join(self.root, name, "sources.tsv")) or b""
        return [tuple(line.split("\t")[:2]) for line in lines(data) if "\t" in line]

    # ----- raw copies, by content hash

    def keep_copy(self, folder, name, data):
        """Archive one raw copy. True if it is new, False if the same bytes were there, None if other bytes are."""
        path = os.path.join(folder, name)
        old = read_or_none(path)
        if old is None:
            make_dirs(folder)
            write(path, data)
        return None if old not in (None, data) else old is None

    def keep_file(self, h, content):
        return self.keep_copy(self.bootefi, h + ".txt", content)

    def keep_record(self, h, raw):
        return self.keep_copy(self.records_dir, h + ".bin", raw)

    def _load(self, path, h):
        data = read(path)
        if sha16(data) != h:
            raise ValueError(f"{path} is not the content it is named after")
        return data

    def file(self, h):
        """The archived file log of that hash, parsed."""
        if h not in self._files:
            self._files[h] = FileLog(self._load(os.path.join(self.bootefi, h + ".txt"), h))
        return self._files[h]

    def record(self, h):
        """(timestamp, record id, message) of the archived error record of that hash."""
        if h not in self._records:
            self._records[h] = cper.decode(self._load(os.path.join(self.records_dir, h + ".bin"), h), h)
        return self._records[h]

    # ----- logs

    def write_log(self, path, rows, check):
        """Write a firmware.log if it changed. True if it was written.

        check: the check of 3.5, run on the candidate read back before it replaces the log that was there."""
        data = merge.log_bytes(rows)
        if read_or_none(path) == data:
            check(data)
            return False
        if self.cfg.fault == "drop-row" and rows:  # test only: the candidate loses its last row before the check
            data = merge.log_bytes(rows[:-1])
        write(path, data, check)
        return True

    def load_unattached(self):
        """{hash: (time, record id, message)} of the rows of unattached/firmware.log."""
        data = read_or_none(self.unattached_log)
        rows = merge.parse(data) if data else []
        return {r[merge.HASH]: (r[merge.TIME], r[merge.NUMBER], r[merge.MESSAGE]) for r in rows}

    def save_unattached(self, records):
        make_dirs(os.path.dirname(self.unattached_log))
        self.write_log(self.unattached_log, merge.unattached(records),
                       lambda data: merge.verify(data, None, None, records))

    def seen_file(self, *fields):
        append(os.path.join(self.bootefi, "seen.tsv"), fields)

    def seen_record(self, action, variable, h, reason="-"):
        """One line more in hwerrrec/seen.tsv. Returns the error if it could not be written: the caller reports
        it, unless the failure that led to a `kept` line is already reported."""
        try:
            append(os.path.join(self.records_dir, "seen.tsv"),
                   (self.cfg.utc, self.cfg.boot_id, action, variable, h, reason))
        except OSError as e:
            return e

    # ----- removal of a run folder (SPEC 4)

    def remove_run(self, name):
        """Remove a run folder in two steps, so that a keeper run stopped in between never leaves half a run
        folder under a run folder's name: renamed first, then deleted."""
        gone = os.path.join(self.root, name + REMOVING)
        os.rename(os.path.join(self.root, name), gone)
        fsync_dir(self.root)
        purge.remove_folder(gone)

    def leftovers(self):
        """Paths of the run folders an earlier keeper run renamed and could not finish removing."""
        try:
            names = sorted(os.listdir(self.root))
        except FileNotFoundError:
            return []
        return [os.path.join(self.root, n) for n in names
                if n.endswith(REMOVING) and FOLDER.fullmatch(n[:-len(REMOVING)])]

    # ----- retention (SPEC 6)

    def prune(self, protected):
        """Remove run folders above the limits, in the order the keeper made them (runs.tsv; a folder it does not
        list counts as made first), never the last one, never one of the keys in `protected`; then the raw file
        logs they alone listed."""
        made = {}
        for line in lines(read_or_none(self.runs_tsv) or b""):
            made.setdefault(line.split("\t")[-1], len(made))
        names = sorted(self.folder_names(), key=lambda n: (made.get(FOLDER.fullmatch(n)[2], -1), n))
        sizes = {n: sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(os.path.join(self.root, n))
                        for f in fs) for n in names}
        dropped = set()
        for name in names[:-1]:
            if len(sizes) <= self.cfg.keep_runs and sum(sizes.values()) <= self.cfg.keep_bytes:
                break
            if FOLDER.fullmatch(name)[2] in protected:
                continue
            dropped.update(h for kind, h in self.listed(name) if kind == "bootefi")
            self.remove_run(name)
            del sizes[name]
        if dropped:
            dropped -= {h for n in sizes for kind, h in self.listed(n) if kind == "bootefi"}
            for h in sorted(dropped):
                try:
                    os.unlink(os.path.join(self.bootefi, h + ".txt"))
                except FileNotFoundError:
                    pass


class Run:
    """One firmware run of the archive: its memory log's lines, the hashes of its file logs and error records."""

    def __init__(self, store, key):
        self.store, self.key, self.name = store, key, store.folders.get(key)
        self.mem = self.mem_hash = self.fpdt = self.esrt = None
        self.problem = self.lost_memory = None  # what stops a save; lost_memory ends when the memory log is read again
        self.files, self.records, self.verified = {}, set(), False  # files: hash -> slot it was first seen in
        if not self.name:
            return
        folder = os.path.join(store.root, self.name)
        memory = None
        try:
            for line in lines(read_or_none(os.path.join(folder, "sources.tsv")) or b""):
                kind, h, count, slot, _ = line.split("\t")
                if kind == "memory":
                    memory = (h, number(count))
                elif kind == "bootefi":
                    self.files[h] = slot
                elif kind == "errrec":
                    self.records.add(h)
        except (OSError, ValueError) as e:
            self.problem = f"{self.name}/sources.tsv: {e}"
        if memory:  # the memory log's lines exist nowhere else than in the log itself
            self.mem_hash = memory[0]
            try:  # no log at all (its first write did not pass the check) holds no line
                log = read_or_none(os.path.join(folder, "firmware.log")) or merge.COLUMNS.encode("utf-8")
                self.mem = merge.memory_lines(merge.parse(log))
                if len(self.mem) != memory[1]:
                    self.lost_memory = f"{self.name}/firmware.log: {len(self.mem)} of its {memory[1]} memory lines"
            except (OSError, ValueError) as e:
                self.lost_memory = f"{self.name}/firmware.log: {e}"

    def offer_memory(self, lines, h):
        """Take a memory dump of this boot, unless the memory log already held has lines the dump lacks: those
        lines exist nowhere else. True if every line of the dump is in the run's memory log afterwards."""
        if (h == self.mem_hash or self.mem is None and not self.lost_memory
                or self.mem is not None and merge.begins(lines, self.mem)):
            self.mem, self.mem_hash, self.lost_memory = lines, h, None
            return True
        return self.mem is not None and merge.begins(self.mem, lines)

    @property
    def empty(self):
        return not (self.name or self.mem is not None or self.files or self.fpdt or self.esrt)

    def render(self):
        """(rows in order, hash of the merged file, tagged, files, records) from the sources as they are now."""
        if self.problem or self.lost_memory:
            raise ValueError(self.problem or self.lost_memory)
        files = {h: self.store.file(h) for h in self.files}
        records = {h: self.store.record(h) for h in sorted(self.records)}  # in hash order: a set's order changes from run to run
        return (*merge.render(self.mem, files, records), files, records)

    def save(self):
        """Write the folder (only the files that changed); the log replaces the one before only if it passes the
        check of 3.5.

        Returns (changed, memory lines, lines of the merged file log, records)."""
        store = self.store
        rows, merged, tagged, files, records = self.render()
        # the label is the first firmware-clock time; without one, the UTC time of the keeper run that made the folder
        label = (merge.fmt_time(rows[0][merge.TIME])[:19].replace(":", "") if rows and rows[0][merge.TIME]
                 else self.name[:17] if self.name else store.cfg.utc[:17])
        name = f"{label}_{self.key}"
        folder = os.path.join(store.root, name)
        changed = name != self.name
        if self.name and changed:
            os.rename(os.path.join(store.root, self.name), folder)
            fsync_dir(store.root)
        elif not self.name:  # a new run: noted first, so that retention knows the order the folders were made in
            append(store.runs_tsv, (store.cfg.utc, store.cfg.boot_id, self.key))
            make_dirs(folder)
        self.name = store.folders[self.key] = name
        sources = []
        if self.mem is not None:
            sources.append(("memory", self.mem_hash, str(len(self.mem)), "-", "-"))
        for h in sorted(files, key=lambda h: (h != merged, h)):
            sources.append(("bootefi", h, str(len(files[h].lines)), self.files[h], "merged" if h == merged else "extra"))
        sources += [("errrec", h, "1", "-", "-") for h in sorted(records)]
        # sources.tsv first: if the log write then fails, the next keeper run finds what the log should hold
        for file_name, data in (("sources.tsv", "".join("\t".join(s) + "\n" for s in sources).encode("utf-8")),
                                ("fpdt-boot.txt", self.fpdt), ("esrt.txt", self.esrt)):
            path = os.path.join(folder, file_name)
            if data is not None and read_or_none(path) != data:
                write(path, data)
                changed = True
        log = os.path.join(folder, "firmware.log")
        old = read_or_none(log)

        def check(data):  # 3.5, and whatever sources.tsv says: the log replaced is the only copy of what it holds
            merge.verify(data, self.mem, files.get(merged), records, tagged)
            if old is not None and old != data:
                merge.keeps(data, old)

        changed |= store.write_log(log, rows, check)
        self.verified = True
        return changed, len(self.mem or ()), len(files[merged].lines) if merged else 0, len(records)
