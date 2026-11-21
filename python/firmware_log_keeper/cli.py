"""The keeper's two commands (SPEC 7) and one keeper run: collect, archive, merge, check, purge (SPEC 1 to 6, 8).

The keeper stores facts and says what it did and what failed. It never comments on the content of the logs.
"""
import lzma
import os
import re
import sys
from datetime import datetime, timezone

from . import advlog, merge, purge, sysfs
from .filelog import PADDING
from .merge import number
from .purge import RECORD_VAR
from .store import FILE_ONLY, FOLDER, Run, Store, make_dirs, read, read_or_none, read_untouched, sha16, write

USAGE = "usage: firmware-log-keeper [migrate <old root>]"
LOCATOR = "AdvLoggerLocator-4d60cfb5-f481-4a98-9c81-bff86460c43e"
BOOT_FOLDER = re.compile(r".+_([0-9a-f]{32})")  # a staging folder or a legacy folder: <UTC time>_<boot id>


class Config:
    def __init__(self, env):
        def get(name, default=None):  # an empty variable counts as unset; a value must be UTF-8 text
            if env.get(name):
                env[name].encode("utf-8")
            return env.get(name) or default

        self.memory_root = get("FWLK_MEMORY_ROOT", "/var/log/firmware_memory")
        self.bootefi_root = get("FWLK_BOOTEFI_ROOT", "/var/log/firmware_bootefilogs")
        self.merged_root = get("FWLK_MERGED_ROOT", "/var/log/firmware")
        self.efivars = get("FWLK_EFIVARS", "/sys/firmware/efi/efivars")
        self.uefilogs = get("FWLK_UEFILOGS") or next(
            (d for d in ("/boot/efi/UefiLogs", "/efi/UefiLogs", "/boot/UefiLogs") if os.path.isdir(d)), None)
        self.devmem = get("FWLK_DEVMEM", "/dev/mem")
        self.fpdt = get("FWLK_SYSFS_FPDT", "/sys/firmware/acpi/fpdt/boot")
        self.esrt = get("FWLK_SYSFS_ESRT", "/sys/firmware/efi/esrt/entries")
        boot_id = get("FWLK_BOOT_ID") or read("/proc/sys/kernel/random/boot_id").decode()
        self.boot_id = boot_id.strip(" \t\r\n").replace("-", "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}", self.boot_id):  # it names folders: anything else would never be found again
            raise ValueError(f"boot id {boot_id.strip()!r} is not 32 hex digits")
        if get("FWLK_NOW") and not re.fullmatch("[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", get("FWLK_NOW")):
            raise ValueError(f"FWLK_NOW {get('FWLK_NOW')!r} is not YYYY-MM-DDTHH:MM:SSZ")
        now = (datetime.strptime(get("FWLK_NOW"), "%Y-%m-%dT%H:%M:%SZ") if get("FWLK_NOW")
               else datetime.now(timezone.utc))
        self.utc = f"{now.year:04d}{now:-%m-%dT%H%M%SZ}"  # the UTC time of this keeper run, as written everywhere
        self.keep_runs = number(get("FWLK_KEEP_RUNS", "300"))
        self.keep_bytes = number(get("FWLK_KEEP_BYTES", str(500 * 1024 * 1024)))
        self.fault = get("FWLK_FAULT")


class Output:
    """Standard output only (the journal): what was done, what was skipped, what failed."""
    failed = False

    def say(self, text):
        try:
            print(re.sub("[ \t\r\n]+", " ", str(text)).strip(" "), flush=True)
        except OSError:  # nobody reads the output (a closed pipe): it is dropped, and the run goes on (SPEC 5)
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())

    def skipped(self, source, reason):
        self.say(f"skipped {source}: {reason}")

    def error(self, step, what):
        self.failed = True
        self.say(f"error: {step}: {what}")


def read_maybe_xz(path):
    """A legacy file, which the legacy keeper compressed above 1 MB. None if neither form exists."""
    data = read_or_none(path)
    if data is None and os.path.exists(path + ".xz"):
        data = lzma.decompress(read(path + ".xz"))
    return data


class Source:
    """What one staging folder or one legacy folder holds: memory dumps, timings and versions of one Linux boot."""

    def __init__(self, path, legacy=False):
        self.path, self.boot = path, BOOT_FOLDER.fullmatch(os.path.basename(path))[1]
        read_dump = read_maybe_xz if legacy else read_or_none  # compressed files and advlog.txt: legacy folders only
        self.dumps = []  # (lines, hash, bytes not decoded, the dump itself if it has such bytes) of each memory dump
        self.unmerged = None  # name of a dump of the folder that cannot be read
        self.held = False  # every line of these dumps is in the run's memory log
        dump = read_dump(os.path.join(path, "advlog.bin"))
        # a keeper run that died between writing a dump and giving it its name: one more dump of the same boot
        left = read_or_none(os.path.join(path, "advlog.bin.tmp"))
        if left is not None and not advlog.whole(left):
            self.unmerged, left = "advlog.bin.tmp", None
        for raw in (dump, left):
            if raw is not None:
                lines, undecoded = advlog.lines(raw)
                self.dumps.append((lines, sha16(raw), undecoded, raw if undecoded else None))
        text = read_maybe_xz(os.path.join(path, "advlog.txt")) if legacy and not self.dumps else None
        if text is not None:  # the hash of a memory log known by its text alone is the hash of that text
            self.dumps.append((advlog.lines_from_text(text.decode("utf-8")), sha16(text), 0, None))
        self.fpdt = read_or_none(os.path.join(path, "fpdt-boot.txt"))
        self.esrt = read_or_none(os.path.join(path, "esrt.txt"))


def boot_folders(root):
    """The staging or legacy folders of a root, in the order of their names' bytes. Never a run folder of the
    archive, whatever the root is."""
    try:
        names = sorted(os.listdir(root), key=os.fsencode)
    except OSError:
        return []
    return [os.path.join(root, n) for n in names
            if BOOT_FOLDER.fullmatch(n) and not FOLDER.fullmatch(n) and os.path.isdir(os.path.join(root, n))]


def resolved(path):
    """The path with its symbolic links resolved for its longest beginning that exists, the rest as written (SPEC 6)."""
    head, tail = os.path.abspath(path), []
    while head != os.sep:
        try:
            return os.path.join(os.path.realpath(head, strict=True), *tail)
        except OSError:
            head, name = os.path.split(head)
            tail.insert(0, name)
    return os.path.join(head, *tail)


def nested(a, b):
    """Are these two folders the same one, or one inside the other?"""
    a, b = resolved(a), resolved(b)
    return os.path.commonpath([a, b]) in (a, b)


def replaces(folder, data, out):
    """May this read of the memory log replace the dumps an earlier keeper run of this boot left in staging? Their
    lines are in no log yet, so only if it begins with every line of each (3.1). A dump cut short under its
    temporary name cannot be compared, and does not count."""
    new = None
    for name in ("advlog.bin", "advlog.bin.tmp"):
        old = read_or_none(os.path.join(folder, name))
        if old is None or old == data or name.endswith(".tmp") and not advlog.whole(old):
            continue
        new, staged = new or advlog.lines(data)[0], advlog.lines(old)[0]
        if not merge.begins(new, staged):
            if not merge.begins(staged, new):  # a read that is the beginning of the staged dump adds nothing
                out.error("memory", f"{folder} kept: the new read lacks lines of the staged dump")
            return False
    return True


def stage(cfg, out):
    """Copy this boot's memory log, timings and versions to staging, where they stay until merged and checked."""
    old = [p for p in boot_folders(cfg.memory_root) if p.endswith("_" + cfg.boot_id)]
    folder = old[0] if old else os.path.join(cfg.memory_root, f"{cfg.utc}_{cfg.boot_id}")

    def memory():
        try:
            locator = read(os.path.join(cfg.efivars, LOCATOR))
        except OSError:
            return out.skipped("memory", "no AdvLoggerLocator variable")
        try:
            return advlog.read_live(cfg.devmem, locator)
        except OSError as e:  # no /dev/mem, or not readable: the source is missing on this machine
            return out.skipped("memory", f"{cfg.devmem}: {e.strerror or e}")

    def timings(step, root, text):
        return text(root) or out.skipped(step, f"nothing in {root}")

    for step, name, collect in (("memory", "advlog.bin", memory),
                                ("fpdt", "fpdt-boot.txt", lambda: timings("fpdt", cfg.fpdt, sysfs.fpdt)),
                                ("esrt", "esrt.txt", lambda: timings("esrt", cfg.esrt, sysfs.esrt))):
        try:
            data = collect()
            if data is not None and (step != "memory" or replaces(folder, data, out)):
                make_dirs(folder)
                write(os.path.join(folder, name), data)
        except Exception as e:  # one failing source never stops the others
            out.error(step, e)


def file_logs(cfg, store, out):
    """Archive every file log of the EFI partition, which is only ever read, and without updating access times.
    Returns [(hash, slot)], one per content."""
    if not cfg.uefilogs or not os.path.isdir(cfg.uefilogs):
        out.skipped("bootefi", "no UefiLogs folder")
        return []
    index = "-"
    try:  # each run of NUL, space, tab, CR and LF is one space, none at the ends
        raw = read_untouched(os.path.join(cfg.uefilogs, "UEFI_Index.txt")) or b""
        index = re.sub("[\0 \t\r\n]+", " ", raw.decode("latin-1")).strip(" ") or "-"
    except Exception as e:  # the file logs are archived all the same
        out.error("bootefi", f"UEFI_Index.txt: {e}")
    found = {}
    for slot in (f"UEFI_Log{n}.txt" for n in range(1, 10)):
        try:
            data = read_untouched(os.path.join(cfg.uefilogs, slot))
            if data is None:
                continue
            content = data.rstrip(PADDING)
            if not content:
                out.skipped(slot, "no content")
                continue
            h = sha16(content)
            new = store.keep_file(h, content)
            if new is None:
                raise ValueError(f"the archive copy {h}.txt holds other bytes")
            if new:
                out.say(f"archived file log {h} from {slot}")
            log = store.file(h)
            store.seen_file(cfg.utc, cfg.boot_id, slot, h, str(len(content)), str(log.trailing),
                            "yes" if log.complete else "no", log.end_written, index)
            found.setdefault(h, slot)
        except Exception as e:
            out.error(slot, e)
    return list(found.items())


def keep(cfg, out, legacy=None):
    """One keeper run. legacy: the old root of the `migrate` command, which imports it instead of this boot."""
    # 0. The three roots must be three separate folders, and the old root a fourth one: staging and legacy folders
    #    are removed once merged, which must never reach the archive itself. None may touch the folders of the
    #    firmware (UefiLogs, efivars): the keeper writes in its roots and removes folders of the old root.
    roots = [cfg.memory_root, cfg.bootefi_root, cfg.merged_root]
    firmware = [p for p in (cfg.uefilogs, cfg.efivars) if p]
    for i, a in enumerate(roots):
        for b in roots[i + 1:] + firmware:
            if nested(a, b):
                return out.error("keeper", f"{a} and {b} are the same folder, or one is inside the other")
    if legacy is not None:
        if not os.path.isdir(legacy):
            return out.error("migrate", f"{legacy} is not a folder")
        if any(nested(legacy, root) for root in roots + firmware):
            return out.error("migrate", f"{legacy} is a folder of the archive or of the firmware, holds one or is "
                                        "inside one")

    store = Store(cfg)
    runs = {}

    def run(key):
        if key not in runs:
            runs[key] = Run(store, key)
        return runs[key]

    def checked(key):
        return key in runs and runs[key].verified

    # 1. Memory logs: this boot's goes to staging first; then every staging folder (those a crashed keeper run
    #    left too) and, in migrate, every legacy folder is one memory log of one Linux boot. A dump never
    #    replaces a memory log that has lines it lacks.
    if legacy is None:
        stage(cfg, out)
    sources = []
    staged = boot_folders(cfg.memory_root)
    for path in staged + (boot_folders(legacy) if legacy is not None else []):
        try:
            sources.append(Source(path, legacy=path not in staged))
        except Exception as e:
            out.error("memory", f"{path}: {e}")
    for s in sources:
        if s.dumps:  # each one is offered: a longer dump of the same boot replaces a shorter one
            s.held = all([run(s.boot).offer_memory(lines, h) for lines, h, _, _ in s.dumps])
        if s.fpdt or s.esrt:
            run(s.boot).fpdt, run(s.boot).esrt = s.fpdt or run(s.boot).fpdt, s.esrt or run(s.boot).esrt
    logged = {s.boot: set(merge.mem_keys(runs[s.boot].mem)) for s in sources if s.dumps}
    for key in store.unfinished:
        run(key)  # a folder an earlier keeper run left without its log: written now from what the folder lists

    try:
        unattached, unattached_ok = store.load_unattached(), True
    except Exception as e:  # unreadable: nothing is added to it, no record depending on it is deleted
        out.error("errrec", f"unattached/firmware.log: {e}")
        unattached, unattached_ok = {}, False

    # 2. File logs (3.1): a file belongs to the memory log that has every one of its row keys (the lowest boot id if
    #    several do); any other file is a run of its own, unless a run folder already lists it.
    alone, taken, absorbed = {}, {}, {}  # hash -> slot; hash -> the boot that has the file; fileonly- key -> its run
    for h, slot in file_logs(cfg, store, out):
        own, file = FILE_ONLY + h, store.file(h)
        boot = min((boot for boot, keys in logged.items() if merge.match(file, keys)), default=None)
        others = store.file_owners.get(h, set()) - {own}
        for key in others:
            run(key)  # seen again: its run is checked again below
        if boot and not others - {boot}:
            taken[h] = boot
            run(boot).files.setdefault(h, slot)
        elif not others:
            alone[h] = slot
    for own in store.folders:  # a fileonly- folder whose file log a boot's folder holds goes into that folder (2.1),
        h = own[len(FILE_ONLY):]  # whether the file is still in UefiLogs or not
        if own.startswith(FILE_ONLY) and h not in alone:
            into = taken.get(h) or min(store.file_owners.get(h, set()) - {own}, default=None)
            if into and h in run(into).files:
                absorbed[own] = into
    for h, slot in alone.items():
        run(FILE_ONLY + h).files.setdefault(h, slot)
    pending = []  # hashes of the error records that need a row
    for own, into in absorbed.items():  # that run reached Linux: its folder goes into the boot's (2.1)
        r, held = Run(store, own), None
        try:  # what the folder holds: the records it lists and those its log has, whatever its sources.tsv says
            log = read_or_none(os.path.join(store.root, store.folders[own], "firmware.log"))
            if not r.problem:
                held = r.records | {row[merge.HASH] for row in merge.parse(log or merge.COLUMNS.encode())
                                    if row[merge.SOURCE] == "errrec"}
        except (OSError, ValueError):
            pass  # not known: the folder stays
        for h in sorted(held or ()):  # in hash order: a set's order changes from run to run
            elsewhere = store.record_owners.get(h, set()) - set(absorbed)
            for key in elsewhere:
                run(key)  # its row there is checked again before the folder goes
            if not elsewhere and h not in unattached:
                pending.append(h)
        absorbed[own] = (into, held)  # the folder goes only once each of these records has a checked row elsewhere
    for key in absorbed:
        runs.pop(key, None)

    # 3. Error records: archived raw by content; one that has no row yet is placed below.
    seen = []  # (variable name, hash) of the records of the firmware whose archive copy is right
    found = []
    if legacy is None:
        try:
            found = [(n, os.path.join(cfg.efivars, n)) for n in sorted(os.listdir(cfg.efivars)) if RECORD_VAR.fullmatch(n)]
        except OSError as e:
            out.skipped("errrec", f"{cfg.efivars}: {e.strerror or e}")
    else:
        old = os.path.join(legacy, "hwerrrec")
        found = [(n, os.path.join(old, n)) for n in (sorted(os.listdir(old)) if os.path.isdir(old) else [])
                 if n.endswith(".bin")]
        # the legacy keeper wrote a copy into the boot folder before the one of its archive: it can be the only one
        found += [(n, os.path.join(p, n)) for p in boot_folders(legacy) for n in sorted(os.listdir(p))
                  if RECORD_VAR.fullmatch(n)]
        try:
            index = read_or_none(os.path.join(old, "index.txt"))
            if index is not None and store.keep_copy(store.records_dir, "legacy-index.txt", index) is None:
                raise ValueError("hwerrrec/legacy-index.txt holds another history")
        except Exception as e:
            out.error("errrec", e)
    copies = {}  # folder -> hashes of the records read from it (None for one that is not archived)
    for name, path in found:
        variable = name if legacy is None else "-"
        copies.setdefault(os.path.dirname(path), []).append(None)
        try:
            raw = read(path)
            h = sha16(raw)
            new = store.keep_record(h, raw)
        except Exception as e:
            out.error("errrec", f"{name}: {e}")
            continue
        if new is None:
            store.seen_record("kept", variable, h, "archive-mismatch")
            out.error("errrec", f"{name} kept: the archive copy {h}.bin holds other bytes")
            continue
        copies[os.path.dirname(path)][-1] = h
        if new:
            failed = store.seen_record("archived", variable, h)
            out.say(f"archived error record {h} from {name}")
            if failed:
                out.error("errrec", f"{name}: seen.tsv: {failed}")
        for key in store.record_owners.get(h, ()):
            if key not in absorbed:
                run(key)
        if not store.record_owners.get(h) and h not in unattached:
            pending.append(h)
        if legacy is None:  # a record imported by migrate is a file of the old root: nothing to delete
            seen.append((name, h))

    # 4. Place the records (3.1): the run whose span holds the timestamp; if several, the one whose span starts
    #    last, then the greatest key. The pending ones are compared with every run. A row of unattached/ was
    #    compared with every run when it was written, so only the runs touched since can take it.
    moving = {}  # hash of a row of unattached/ -> key of the run that has it now
    for h in unattached:
        for key in sorted(store.record_owners.get(h, set()) - set(absorbed))[:1]:
            moving[h] = key  # a keeper run died between writing it there and removing it here
            run(key)
    pending = list(dict.fromkeys(pending))
    waiting = [h for h in unattached if h not in moving and h not in pending and unattached[h][0]]
    if pending or waiting:
        spans = {}
        for key in (set(store.folders) - set(absorbed) if pending else set()) | set(runs):
            try:
                rows = runs[key].render()[0] if key in runs else merge.parse(
                    read(os.path.join(store.root, store.folders[key], "firmware.log")))
                spans[key] = merge.span(rows)
            except Exception:
                spans[key] = None  # reported when that run is saved, or not this keeper run's business
        unattached_before = dict(unattached)
        for h in pending + waiting:
            try:
                time = store.record(h)[0] if h in pending else unattached[h][0]
                inside = [(span[0], key) for key, span in spans.items()
                          if span and time and span[0].replace(microsecond=0) <= time <= span[1]]
                if inside:
                    store.record(h)  # the run's log is made from the archive copy: it must be readable
                    run(max(inside)[1]).records.add(h)
                    if h in unattached:
                        moving[h] = max(inside)[1]
                elif unattached_ok and h in pending:
                    unattached[h] = store.record(h)
            except Exception as e:
                out.error("errrec", f"{h}: {e}")
        if unattached != unattached_before:
            try:
                store.save_unattached(unattached)
            except Exception as e:
                out.error("errrec", f"unattached/firmware.log: {e}")
                unattached_ok = False

    # 5. Write the runs touched; each log replaces the one before only if it passes the check of 3.5. A new run is
    #    noted in runs.tsv when it is saved: the boots of the staging and legacy folders first, in the order of
    #    their folder names (the UTC time they were collected at), then the other runs by key.
    for key in dict.fromkeys([s.boot for s in sources if s.boot in runs] + sorted(runs)):
        r = runs[key]
        if r.empty:
            continue
        try:
            changed, mem_lines, file_lines, records = r.save()
            if changed:
                out.say(f"merged run {key} into {r.name} (memory {mem_lines} lines, bootefi {file_lines} lines, "
                        f"errrec {records})")
        except Exception as e:
            out.error("merge", f"{key}: {e}")
    moved = [h for h, key in moving.items() if checked(key) and h in runs[key].records]
    if moved and unattached_ok:  # their rows are checked in a run's log: they leave unattached/
        try:
            store.save_unattached({h: rec for h, rec in unattached.items() if h not in moved})
            for h in moved:
                del unattached[h]
        except Exception as e:
            out.error("errrec", f"unattached/firmware.log: {e}")

    # 6. Purges (4), each one only for what is written, read back and checked.
    def remove(path):
        try:
            purge.remove_folder(path)
        except OSError as e:
            out.error("purge", e)

    def has_row(h):
        return any(r.verified and h in r.records for r in runs.values()) or unattached_ok and h in unattached

    def keep_raw(s):
        """Copy the dumps of a staging or legacy folder that have bytes the decoder could not read into the run
        folder: the dump is the only copy of those bytes. False if one could not be copied."""
        folder = runs[s.boot].name
        for _, h, undecoded, raw in s.dumps:
            if undecoded:
                try:
                    if store.keep_copy(os.path.join(store.root, folder), f"advlog-{h}.bin", raw) is None:
                        raise OSError(f"{folder}/advlog-{h}.bin holds other bytes")
                except OSError as e:
                    out.error("memory", f"{s.path} kept: {e}")
                    return False
                out.error("memory", f"{s.path}: {undecoded} bytes not decoded, dump kept as {folder}/advlog-{h}.bin")
        return True

    for path in store.leftovers():  # run folders an earlier keeper run could not finish removing
        remove(path)
    for key, (into, held) in absorbed.items():  # before the staging folders: a failure here can be done again
        if held is None:
            out.error("purge", f"{store.folders[key]} kept: its sources.tsv or its log cannot be read")
        elif not checked(into):
            continue
        elif all(has_row(h) for h in held):
            try:
                store.remove_run(store.folders.pop(key))
            except OSError as e:
                out.error("purge", e)
        else:
            out.error("purge", f"{store.folders[key]} kept: a record of it has no checked row in another log")
    for s in sources:
        if s.unmerged:  # with or without timings and versions beside it
            out.error("memory", f"{s.path} kept: {s.unmerged} is not a whole dump, not merged")
        elif not (s.dumps or s.fpdt or s.esrt):
            if os.listdir(s.path):
                out.error("memory", f"{s.path} kept: nothing readable in it")
            else:
                remove(s.path)  # an empty staging folder, left by a keeper run that died at once
        elif checked(s.boot) and not (s.dumps and not s.held):
            if not all(h and has_row(h) for h in copies.get(s.path, ())):
                out.error("errrec", f"{s.path} kept: a record copy of it has no checked row in a log")
            elif keep_raw(s):
                remove(s.path)
        elif checked(s.boot):
            out.error("memory", f"{s.path} kept: boot {s.boot} already has a memory log with other lines")
    for name, h in seen:
        if not has_row(h):
            store.seen_record("kept", name, h, "not-verified")
            out.error("errrec", f"{name} kept: its row is not checked in a log")
            continue
        try:
            purge.delete_variable(cfg.efivars, name)
        except OSError as e:
            store.seen_record("kept", name, h, "delete-failed")
            out.error("errrec", f"{name} kept: {e}")
            continue
        failed = store.seen_record("deleted", name, h)
        out.say(f"deleted error record {name}")
        if failed:
            out.error("errrec", f"{name}: seen.tsv: {failed}")

    try:  # never a run written or seen again in this keeper run (its sources may be gone, or still in UefiLogs),
        store.prune(set(runs) | set(absorbed))  # never a fileonly- folder this keeper run decided to keep
    except OSError as e:
        out.error("retention", e)


def main(argv):
    if sys.stdout:  # a file name's bytes go out as they are, whatever the locale (SPEC 5)
        sys.stdout.reconfigure(encoding="utf-8", errors="surrogateescape")
    out = Output()
    if argv and (argv[0] != "migrate" or len(argv) != 2 or not argv[1]):
        out.say(USAGE)
        return 2
    try:
        keep(Config(os.environ), out, legacy=argv[1] if argv else None)
    except Exception as e:  # nothing goes to standard error
        out.error("keeper", f"{type(e).__name__}: {e}")
    return 1 if out.failed else 0
