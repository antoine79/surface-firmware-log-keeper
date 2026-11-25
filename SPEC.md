# firmware-log-keeper: specification

Two small programs for machines with Project Mu UEFI firmware (Microsoft Surface devices and other Mu-based machines), whose firmware logs no Linux distribution saves today.

- **`firmware-log-keeper`** (the collector): runs once per Linux boot as root. It collects what the firmware left behind, archives it, merges everything into one de-duplicated log per firmware run, and frees the firmware's error-record storage once its content is safely archived. **It collects and stores; it does not judge.** No alerts, no thresholds, no statistics.
- **`firmware-log-report`** (the analysis): reads the archive, never writes to it. Everything that interprets the logs lives here (section 8). Built after the keeper.

The contract between the two is the archive layout of section 2: the keeper records facts, complete and unmodified, so that any analysis can be done later from the archive alone.

Written in Rust (one binary per program), with a black-box test suite in Python (pytest).

**Vocabulary.** A *firmware run* is one execution of the firmware, from the processor leaving reset to the hand-over to the boot loader (or to a hang). The firmware has no identifier for a run and logs no "start" message; a run is known only through what it leaves: one memory log, one file log, some error records, or nothing. *Uptime* is the time since reset within a run. *Firmware clock* is the hardware clock as the firmware reads it; the keeper stores its values as found and never converts or corrects them.

## 1. Sources (what the firmware leaves)

| Source | Where | Lifetime | Keeper action |
|---|---|---|---|
| **Memory log** | Advanced Logger buffer in reserved RAM; its physical address is in the UEFI variable `AdvLoggerLocator-4d60cfb5-f481-4a98-9c81-bff86460c43e` (bytes 4-11, little-endian u64, after the 4 attribute bytes); read through `/dev/mem` | emptied at every reset; holds the current run only | read, never written |
| **File log** | `<ESP>/UefiLogs/UEFI_Log<1-9>.txt` and `UEFI_Index.txt`, written by the firmware at the hand-over to the boot loader (only if the `UefiLogs` folder exists), one file per run | 9 files of 4 MB in rotation, overwritten oldest first | **read only. The keeper never writes, renames or deletes anything on the EFI partition**, and opens its files without updating their access time (`O_NOATIME`; a plain read if the system refuses that flag): that update would be a write to the partition |
| **Error records** | UEFI variables `HwErrRec<4 hex digits>-414e6bdd-e47b-47cc-b244-bb61020cf516` (4 attribute bytes + CPER record) | the firmware takes the first free slot and never frees one; when all are taken, new errors are dropped | archived raw, then **deleted** once the archive copy matches byte for byte |
| `HwErrRecSupport-8be4df61-93ca-11d2-aa0d-00e098032b8c` | UEFI variable | permanent | **never touched** |
| Boot timings | `/sys/firmware/acpi/fpdt/boot/*` | per Linux boot | copied |
| Firmware versions | `/sys/firmware/efi/esrt/entries/*/{fw_class,fw_version,last_attempt_version,last_attempt_status}` | per Linux boot | copied |

Every source may be missing on a given machine: the keeper skips it, says so in its output, and still processes the others. One failing source never stops the others.

### 1.1 Memory log

Layout from mu_plus `AdvLoggerPkg/Include/AdvancedLoggerInternal.h`: a header (signature `ALOG`), then entries `ALMS` or the newer `ALM2`. The header gives the timer frequency and one reading of the firmware clock (year to second) with the timer tick at which it was read; each entry gives a timer tick, a debug level (32-bit) and text. So every line has an uptime (tick × 1000 / frequency, rounded down to the millisecond, as the firmware does when it writes the file log) and a firmware-clock time (the clock reading, plus the line's tick minus the clock's tick, that difference converted and rounded down the same way). An entry's text may span several lines; each line is its own log line with the entry's tick and level. A line built from several entries takes the tick and level of the entry that ends it; a last line without a line break takes those of the last entry. (The firmware stamps its file log with the last one: on the Surface Book 2 data, 1 to 5 lines per boot fail to pair otherwise.) Text is read as Latin-1.

The dump kept in staging, `advlog.bin`, is the header and the used entries: the bytes from the header's address to the write position. Its sha256 is the memory log's content hash.

The memory log is the only source with no raw copy in the archive, so the bytes of a dump that the decoder does not turn into lines are counted: the bytes skipped to reach the next `ALMS` or `ALM2` signature when what stands at an entry's place is not an entry the decoder knows, and the bytes left before the write position when it finds no further entry. The lines decoded are merged as usual. When that count is not zero, the dump is copied raw into the run folder as `advlog-<its hash>.bin` before its staging folder is removed, and the keeper run reports one `error: memory:` line giving the count and that file.

### 1.2 File log

- 4 MB, padded after the content with spaces, NUL bytes and line breaks (the Surface Book 2 pads with lines of 70 spaces); CRLF line endings: a line ends at LF, and one CR just before it is not part of the line. The content is the file without its trailing run of space, NUL, CR and LF bytes. A file with no content (empty, or padding only) is skipped with one message: nothing archived, no `seen.tsv` line.
- Each content line is `HH:MM:SS.mmm : <message>` (uptime). The message may be empty, and the space after the colon may then be missing. A blank line is not a log line, but it counts in the line numbers: `file_line` is the number an editor shows.
- A flushed log ends with two blank lines and ` === END_OF_LOG === @ === YYYY/M/D H:MM:SS ===` (firmware clock at the hand-over, to the second). That line is not a log line.
- **Only the content up to and including the first `END_OF_LOG` line belongs to that log.** Anything after it (a stale tail of an older, longer log in the same slot) is not merged; its size (the content bytes after the first `END_OF_LOG` line) is recorded.
- A file without `END_OF_LOG` is incomplete: archived and merged all the same, recorded as incomplete.
- A line that does not parse is kept, with the uptime of the previous line and its raw text. It is in no memory log, so its file is a run of its own (3.1).
- Firmware-clock time of a file line, when its run has no memory log: `END_OF_LOG` clock minus the uptime of the last content line, plus the line's uptime. Without `END_OF_LOG` there is no clock: the time column holds the uptime only (see 3.2).
- An `END_OF_LOG` clock that is not a date and time of the years 1 to 9999 counts as no clock, and so does one from which a row time of the run would leave those years. (Likewise a memory-log line whose time would leave those years has no time.)

### 1.3 Error record

Raw variable content = 4 attribute bytes + CPER record. Fields read (format decoding only): record id, timestamp (firmware clock, to the second; the two-digit year is used as 20YY and the raw century byte is kept in the text), severity, and for each section its type, and for a Project Mu telemetry section the component and subcomponent identifiers, the status code and the two information values. A variable that is not a complete CPER record (no `CPER` signature, or shorter than its header and section table say) is archived raw all the same and gets a row without time (3.2).

## 2. Archive layout (the contract)

All roots are configurable (section 6). Defaults:

```
/var/log/firmware_memory/        staging only: raw dumps of the current Linux boot, removed once merged
  <UTC time of the keeper run>_<Linux boot id>/advlog.bin, fpdt-boot.txt, esrt.txt
/var/log/firmware_bootefilogs/   raw archive: every distinct UEFI_Log content ever seen
  <sha256 of the content, 16 hex>.txt      content = the bytes before the padding, unmodified
  seen.tsv                                 append-only (see below)
/var/log/firmware/               the merged, permanent log
  <first firmware-clock time of the run, %Y-%m-%dT%H%M%S>_<run key>/
    firmware.log                 merged rows (section 3)
    sources.tsv                  what went into it (see below)
    fpdt-boot.txt, esrt.txt      only for runs that reached Linux
    advlog-<sha256 16 hex>.bin   only for a memory dump with bytes the decoder could not read (1.1), raw
  unattached/firmware.log        rows of error records that fall inside no archived run (same row format)
  hwerrrec/<sha256 16 hex>.bin   every distinct error record ever seen, raw
  hwerrrec/seen.tsv              append-only (see below)
```

`<run key>`: the Linux boot id (32 hex) for a run whose memory log, timings or versions the keeper collected in that boot, else `fileonly-<sha256 16 hex of the file log content>`: a run known by its file log only. That name states what the keeper has, not what happened: the run may have hung before Linux, or the keeper may have been unable to read the memory log (section 8). The time in the folder name is only a readable, sortable label; identity is the run key. A run with no firmware-clock time when its folder is made (a file log without `END_OF_LOG`, or a Linux boot whose memory log could not be read) takes the UTC time of that keeper run. A folder's name never changes. A Linux boot gets its folder as soon as the keeper has its memory log, its timings or its versions, even if `firmware.log` then holds the column line only.

- `firmware_bootefilogs/seen.tsv`, one line per file per keeper run: UTC time of the keeper run, Linux boot id, slot (file name), content hash, content length, trailing bytes after the first `END_OF_LOG`, complete (yes/no), `END_OF_LOG` clock as written, value of `UEFI_Index.txt` (the file read as Latin-1, each run of NUL, space, tab, CR and LF bytes replaced by one space, without a space at either end; `-` if nothing is left, if the file is missing or if it cannot be read, which is also reported as `error: bootefi:`).
- `sources.tsv`, one line per source merged into the run: kind (`memory` | `bootefi` | `errrec`), content hash, line count, and for `bootefi` the slot and whether it was the copy merged or an extra matching copy.
- `hwerrrec/seen.tsv`, one line per event: UTC time of the keeper run, Linux boot id, action (`archived` | `deleted` | `kept`), variable name (the file name in efivars), content hash, reason (for `kept`: `archive-mismatch`, `not-verified` or `delete-failed`; else `-`).

Formats: the `.tsv` files have no column line, their fields are separated by tabs and an absent value is `-`. The UTC time of the keeper run is written `YYYY-MM-DDTHHMMSSZ` everywhere (staging folder name included), with four digits of year. In `firmware_bootefilogs/seen.tsv` the lines of one keeper run are in slot order, and the `END_OF_LOG` clock is copied as the firmware wrote it (`2026/1/2 3:04:05`; tabs as spaces, without the spaces around it). `sources.tsv` has five fields (kind, hash, line count, slot, `merged` or `extra`), with `-` in the last two for `memory` and `errrec` and a line count of 1 for `errrec`; its lines are in the order memory, bootefi (the merged copy, then the extra copies by hash), errrec (by hash). `fpdt-boot.txt` holds one `<file name>=<value>` line per file, sorted by name; `esrt.txt` one line per entry, sorted, `<entry> fw_class=… fw_version=… last_attempt_version=… last_attempt_status=…`. Each value is read as UTF-8 (invalid bytes replaced) without white space at either end. A folder, file or field that cannot be read is an error of that source (`error: fpdt:` or `error: esrt:`), and nothing of it is staged. In the staging root, a name that is not `<UTC time of the keeper run>_<Linux boot id>` is left alone.

What the keeper reads back must be in the format it writes. A run whose `sources.tsv` or `firmware.log` is not keeps its folder as it is and reports `error: merge:`; an `unattached/firmware.log` that is not gets no new row, and no record whose row it should hold is deleted. A name of the merged root that is not `<time label>_<run key>` with ASCII digits in its label is not a run folder, and the keeper leaves it alone. A merged root that cannot be listed stops every merge and purge of the keeper run (`error: keeper:`); the file logs and error records are still archived raw where they can be.

### 2.1 No duplicates

- `firmware_bootefilogs/`: one file per distinct content. Seeing the same unrotated file again adds a `seen.tsv` line and nothing else.
- `firmware/`: one folder per firmware run, found by its run key (and, for a file log, by its content hash in the `sources.tsv` files). Re-running the keeper, or seeing the same file again, finds the existing folder. The keeper never removes or renames a run folder: a file log stays in the folder that first listed it, a `fileonly-` one too when the memory log of its run is read later.
- `firmware/hwerrrec/`: one `.bin` per distinct record (sha256 of the whole variable content). A record that has its row in a log is never added a second time. An archive copy whose row is missing (an earlier run failed in between) gets its row at the next run that sees the record. A row of `unattached/firmware.log` stays there.

## 3. Merge

### 3.1 Which sources belong to one run

- **Memory log:** a keeper run has the current run's, plus those of staging folders left by a crashed earlier run (4). Each belongs to the Linux boot it was read in. When that boot's folder already holds a memory log (an earlier keeper run of the same boot), the lines of the archived one exist nowhere else, so a dump replaces it only if it begins with every archived line (every memory line its `firmware.log` holds, whatever `sources.tsv` lists): same uptime and text, line by line, where the last archived line may be the unfinished form of its line in the dump (same or later uptime, text that begins with the archived text). A dump that is itself the beginning of the archived log adds nothing and is dropped. Any other dump is kept in staging and reported as an error; the archived log stays. A dump still in staging (its merge failed, or the keeper run was stopped) is in the same position: a later read of the same boot replaces it there only if it begins with every line of it, by the same rule; a read that is itself the beginning of the staged dump is dropped; any other read is dropped and reported as an error, and the staged dump stays and is merged.
- **File log:** a file log a run folder already lists stays in that run. A new one belongs to the run of one of these memory logs only when every one of its row keys (3.3) is among that memory log's row keys. There is no proportion of lines, no clock and no tolerance of any kind: the firmware writes the file from its memory buffer, so the file log of a run is an exact copy of the beginning of that run's memory log (on the Surface Book 2 data the 8 true pairs share all their keys, and a file shares at most 82 % with another boot). The text is compared as it is first; if that comparison is not complete, it is made again without the tags of newer Project Mu firmware (3.3), and that one too must be complete. If several memory logs hold every key of the file: the lowest boot id. Every other new file log is another run and gets its own `fileonly-` folder. If two files belong to the same memory log, the one with more lines is merged (the lowest content hash if they have as many) and both are listed in `sources.tsv`; the other is an extra copy and adds no row, since each of its lines is already a row of the memory log.
- **Error record:** a record without a row is placed once, in the archived run whose firmware-clock span (the earliest to the latest time among its log rows, wherever they are in the log, both included, compared to the second) contains the record's timestamp; if several runs qualify, the one whose span starts latest, then the greatest run key compared as text. A record inside no run goes to `unattached/firmware.log` and stays there. This is an exact comparison of two readings of the same clock, with no tolerance. (Records written by a run that hung inside the firmware belong to no archived run: such a run leaves no log.)

### 3.2 Rows

`firmware.log` is tab-separated, first line naming the columns, then one row per line or record:

```
time    uptime_s    seg    level    source    mem_line    file_line    message
```

- `time`: firmware-clock time of the row, `YYYY-MM-DDTHH:MM:SS.mmm`, as found (no zone, no correction). For a file-only run without `END_OF_LOG`: `-`. The reset time of a run that has a memory log is the time of its first `both` or `memory` row, in the order of 3.4, minus that row's uptime (both are rounded down on their own, so this is not always the clock reading minus its tick); for a file-only run it is the time 1.2 gives to uptime 0.
- `uptime_s`: seconds since reset, 3 decimals; `-` for an error-record row in `unattached/`. For an attached error record: its timestamp minus the run's reset time. That can be negative, for a record of the run's first second when the reset time has a millisecond part: it is written with a minus sign (`-0.500`) and the row sorts before the others.
- `seg`: segment number within its source, starting at 0, incremented each time the uptime of a line is lower than that of the line before it in the same source (a log that would hold more than one run). Always 0 on all data seen so far.
- `level`: `L%08x` from the memory copy, else `-`.
- `source`: `both`, `bootefi`, `errrec` or `memory`. A run that has a memory log has `both`, `memory` and `errrec` rows only; `bootefi` rows are those of a file-only run.
- `mem_line`, `file_line`: the line's number in its source (1-based), else `-`.
- `message`: the text, trailing spaces, tabs, CR and LF removed (these four characters and no other: a form feed or a non-breaking space at the end stays), tabs replaced by spaces; source bytes are read as Latin-1 and the file is written in UTF-8, with LF line ends. The row key (3.3) uses this same text. For `errrec`: `HwErrRec record_id=0x<id> severity=<name> hash=<16 hex> century=<raw>` followed by one ` | section …` part per section with the fields of 1.3: ` | section type=<identifier>`, and for a Project Mu telemetry section ` | section type=<identifier> component=<identifier> subcomponent=<identifier> status=0x<8 hex> info1=0x<hex> info2=0x<hex>`. The record id and the information values are in hex without leading zeros, the century is two hex digits, the severity is `recoverable`, `fatal`, `corrected` or `informational` (else the number), identifiers are in the usual lower-case form. A record that cannot be decoded (1.3): `HwErrRec undecoded hash=<16 hex> length=<bytes of the variable>`, with `-` as its time, in `unattached/firmware.log`.

### 3.3 Row key and de-duplication

The key of a log line is (`seg`, uptime in milliseconds, message, occurrence), where occurrence counts earlier lines of the same source with the same first three values (1 for the first). Timestamps alone are not unique (on the Surface Book 2, 62 % of lines share their millisecond with an earlier line, and identical lines repeat), the full key is unique by construction.

- a key present in both the memory and the file copy: one row, source `both`, level from memory, both line numbers;
- a key of the memory log only: one row, source `memory` (a line written after the file log, or a run whose file log is not there);
- every row of a file-only run: source `bootefi`;
- a file log with even one key the memory log lacks (a line with other text, a garbled line, a line cut by an interrupted write) is not of that run (3.1): it is another run, in its own `fileonly-` folder, and the boot's folder holds the memory lines alone. The keeper does not judge the difference.

Newer Project Mu firmware writes tags before the text of each line of the file log, and only there: `HH:MM:SS.mmm : [DXE  ] [INFO] text`. A phase tag is `[`, 5 characters, `]` (or `[`, 4 hex digits, `]` for a phase without a name); a level tag is `[`, 4 characters, `]`; each is followed by a space or ends the line; the phase tag, the level tag or both may be missing. A file log is first compared with a memory log as it is. Only if not every one of its keys is among the memory log's that way, its keys are computed again without the tags (told by their shape alone: a leading phase tag, then a leading level tag), and the file is of that run if every one of them is among the memory log's then (3.1). Every row of such a file is then `both` and holds the text of the memory copy; a file-only run keeps the file's whole text, tags included. The raw copy always has the tags. Because the tags are told by their shape, a line the firmware wrote without a level tag whose text begins like one (`[HDD0] ready`) loses that beginning too, and its file is then another run.

An error record's key is its content hash.

### 3.4 Order

Rows are written sorted by `seg`, then `uptime_s`, then `source` (alphabetical: `bootefi`, `both`, `errrec`, `memory`), then line number (`mem_line`, else `file_line`, else record id, then the record's hash). In `unattached/firmware.log`: by `time`, then record id, then hash. The order is total: the same inputs always give the same file, byte for byte.

### 3.5 Verification before purge

A new `firmware.log` (a run's, or `unattached/`'s) is written to a temporary file, flushed, read back and checked: every line number of every merged source, and every attached record, appears exactly once with the same uptime and text as in the source (for a file log paired without its tags: the file's text without its tags). Only if the check passes does it replace the log that was there, which is otherwise left untouched: for an older run that log is the only copy of the memory lines. For the same reason a run's new log never replaces one that holds a memory line or an error-record row it lacks, whatever `sources.tsv` lists: the memory rows of the log replaced must be the beginning of the new log's (rule of 3.1) and each of its error records must have its row in the new log; otherwise the old log stays and the keeper run reports `error: merge:`. A run folder found without a `firmware.log` (its first log failed the check, or the keeper run was stopped) gets its log at the next keeper run, from the sources its `sources.tsv` lists. Only then may staging data be removed or a record be deleted from the firmware. A log in which a memory row has lost its uptime or its level (`-`) cannot be compared with a dump: it counts as unreadable, so its run reports `error: merge:` and keeps that log, and the keeper run goes on with the other runs.

## 4. Purges

Every purge happens only after the archive copy has been written, flushed (`fsync`), read back and compared. Each folder made in the archive is flushed too (`fsync` of its parent folder). A failure keeps the source and is recorded (`error: purge:` for a folder that cannot be removed). A symbolic link in a folder being removed is removed, never followed. The keeper deletes nothing else: nothing in the merged archive, nothing in the raw file-log archive.

1. **Error records:** for each `HwErrRec<nnnn>-414e6bdd-…` whose bytes equal `firmware/hwerrrec/<hash>.bin` exactly and whose row is verified in a `firmware.log`, remove the immutable attribute by running the `chattr` program found in `PATH` with the two arguments `-i <path of the variable>` (a failure of `chattr` itself is ignored) and delete the file in the efivars directory. The path deleted is always the efivars directory plus a name of that exact form. Record it in `hwerrrec/seen.tsv`; if that line (or the `archived` line) cannot be written, the deletion stays done and the keeper run reports `error: errrec:` with the variable's name. Never touch any other variable.
2. **Staging (`firmware_memory/`):** once the memory log, timings and versions are in their run folder and verified, delete that staging folder. A staging folder left by a crashed earlier keeper run is merged and removed by the next one. A file left under a temporary name (`*.tmp`) is never read and goes with its folder. A staging folder whose `advlog.bin` cannot be decoded is kept whole and reported as an error; so is a staging folder with other files and nothing readable, and one whose timings or versions are not those its run keeps (of two staging folders of one boot, the run keeps the later one's).
3. **EFI partition: never.**

## 5. Keeper output

Operational only, to standard output (the journal): what it did and what failed.

- `merged run <key> into <folder> (memory <n> lines, bootefi <n> lines, errrec <n>)`
- `archived file log <hash> from <slot>` / `archived error record <hash> from <variable>` / `deleted error record <variable>`
- `skipped <source>: <reason>`
- `error: <step>: <what>`

In `skipped` and `error` lines the source or step is `memory`, `bootefi`, `errrec`, `fpdt`, `esrt`, or a slot's file name for one file log; in `error` lines it can also be `merge` (writing or checking a run's log), `purge` (removing a folder) or `keeper` (any failure outside a step, a wrong setting included). `<folder>` is the run folder's name; a variable is named by its file name in efivars. A record kept in the firmware (4) is reported with an `error` line naming its variable. An `error` line about a staging folder that is kept contains that folder's path; one about a run folder that is kept contains its name. Nothing is written to standard error.

`merged run` is printed for each run folder made or changed in this keeper run; a run whose files already hold those bytes prints nothing. In an `error` line only `error: <step>:` is fixed: the text after it is free. Every other line is fixed to the byte. A printed line has each run of white space (space, tab, CR, LF) replaced by one space, with none at either end. Output that cannot be written (nobody reads the pipe) is dropped, and the run goes on. A usage error (section 7) prints one line beginning `usage: `, whose text is free.

Exit status 1 if any step raised an error, else 0. Nothing about the content of the logs.

## 6. Configuration

Environment variables (defaults in brackets), so that tests run on scratch directories:

`FWLK_MEMORY_ROOT` [/var/log/firmware_memory], `FWLK_BOOTEFI_ROOT` [/var/log/firmware_bootefilogs], `FWLK_MERGED_ROOT` [/var/log/firmware], `FWLK_EFIVARS` [/sys/firmware/efi/efivars], `FWLK_UEFILOGS` [first existing of /boot/efi/UefiLogs, /efi/UefiLogs, /boot/UefiLogs], `FWLK_DEVMEM` [/dev/mem], `FWLK_SYSFS_FPDT` [/sys/firmware/acpi/fpdt/boot], `FWLK_SYSFS_ESRT` [/sys/firmware/efi/esrt/entries], `FWLK_BOOT_ID` [read from /proc; either way white space around it and dashes are removed and letters put in lower case, and anything else than 32 hex digits is an error], `FWLK_NOW` [current time; for tests, UTC as exactly `YYYY-MM-DDTHH:MM:SSZ`, a real date and time, else an error], `FWLK_FAULT` [unset; for tests only]. A variable that is set but empty counts as unset. A value that is not UTF-8 text is an error. An error in a setting stops the keeper with one `error: keeper:` line before it reads or writes anything.

`FWLK_FAULT=drop-row` forces the failure that the check of 3.5 exists for: each `firmware.log` written in this keeper run loses its last row in the temporary file, before it is checked. The check must then fail, the log that was there stays, the run reports the error and exits 1, and nothing that depended on that log is purged.

The three roots must be three separate folders, none inside another, and none may be the `UefiLogs` folder or the efivars folder, inside one of them or around one of them: otherwise the keeper stops with one `error: keeper:` line before reading or writing anything. The paths are compared as absolute paths, as written; symbolic links are not followed.

## 7. Keeper command

`firmware-log-keeper` with no argument is one keeper run (what the unit does). Any argument is a usage error: exit status 2, nothing read or written.

## 8. `firmware-log-report` (separate program, read-only)

Reads the three roots (the same `FWLK_*_ROOT` variables and defaults as the keeper, section 6) and prints to standard output; writes nothing. The archive is world-readable, so it needs no root. Each item is derived from the archive alone.

```
firmware-log-report [--stage-over SECONDS] [--repeat-over COUNT]   the summary below (defaults 30 s and 50)
firmware-log-report timeline                                        every row of every run and of unattached/, by time
firmware-log-report decode-advlog FILE                              a raw memory dump, decoded as the keeper does (1.1)
```

The summary, in this order:

- list of firmware runs, one line each: run folder, sources (from `sources.tsv`), rows per source, lines found in one copy only (the `memory` rows of a run that has both copies; `-` with one copy), and the duration of the firmware stage: the uptime of the run's last `both` or `bootefi` row (the file log is written at the hand-over to the boot loader), else `bootloader_launch_ns` of `fpdt-boot.txt`;
- runs known by their file log only (`fileonly-` folders). Such a run never reached Linux only if the keeper could read memory logs then: the keeper run that first saw the file (first line of its hash in `firmware_bootefilogs/seen.tsv`) has a `memory` line in the `sources.tsv` of its own boot's folder. Otherwise the report says only that the memory log was not read. A file first seen by the first keeper run of `seen.tsv` was written before any keeper ran, so the report says it may have reached Linux. "Never reached Linux" means no Linux boot that ran the keeper: the run may have hung in the firmware or started another system;
- error records: every record that has a row (in a run or in `unattached/`) or a line in `hwerrrec/seen.tsv`, with its time, the first time of each action of `hwerrrec/seen.tsv` (archived, deleted, kept with its reason), where its row is, and its row's text; the heading counts those attached to no run;
- rotation gaps: from `firmware_bootefilogs/seen.tsv`. `UEFI_Index` names the slot the firmware wrote last (1 to 9), so between two keeper runs it wrote as many file logs as the index moved, modulo 9. When fewer new contents than that were seen, the rest were overwritten unseen. When all 9 were new, the firmware may have gone round more than once: the report gives the smallest number lost, "or more";
- firmware stages longer than `--stage-over` seconds, and lines (other than error-record rows) repeated more than `--repeat-over` times in one run, most repeated first. The defaults come from the Surface Book 2 data: a normal run hands over at about 11 s and repeats no line more than 8 times;
- a staging root that is not empty (the next keeper run merges it).

A file that cannot be read or is not in the keeper's format gives one `error: <path>: <reason>` line at the end and exit status 1; the rest is still printed. A usage error prints the usage, beginning `usage: `, with exit status 2.

Thresholds, wording and output formats of the report can change freely without touching the keeper or the archive.

## 9. Packaging

```
README.md, SPEC.md, DESIGN.md, TEST-SCENARIOS.md
tests/                        the pytest suite, synthetic fixtures only; runs the keeper named by FWLK_BIN
  test_realdata.py            optional, runs only when FWLK_REALDATA points to a local snapshot
rust/                         Cargo workspace; the standard library and sha2 only, no unsafe code
  firmware_logs/              library: the keeper's four steps, and the formats the report shares
  firmware-log-keeper/        the keeper binary: one command, a run
  firmware-log-report/        the report binary (section 8)
systemd/firmware-log-keeper.service   the unit below
install.sh                    install | status | undo, as root, from the project folder
LICENSE                       MIT
```

Both are built with `cargo build --release` in `rust/`; `install.sh` installs the keeper as `/usr/local/sbin/firmware-log-keeper` and the report as `/usr/local/bin/firmware-log-report`.

Nothing in the code is specific to one machine: no addresses, slot counts or device names. The unit (`systemd/firmware-log-keeper.service`):

```
[Unit]
Description=Keep the firmware's own logs (Advanced Logger, UefiLogs, error records, boot timings, versions)
Documentation=https://github.com/antoine79/firmware-log-keeper
After=local-fs.target
# the EFI partition is read from the first of these that holds UefiLogs
RequiresMountsFor=/var/log /boot/efi /efi /boot

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/firmware-log-keeper
Nice=10

[Install]
WantedBy=multi-user.target
```
