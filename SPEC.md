# firmware-log-keeper: specification

Two small programs for machines with Project Mu UEFI firmware (Microsoft Surface devices and other Mu-based machines), whose firmware logs no Linux distribution saves today.

- **`firmware-log-keeper`** (the collector): runs once per Linux boot as root. It collects what the firmware left behind, archives it, merges everything into one de-duplicated log per firmware run, and frees the firmware's error-record storage once its content is safely archived. **It collects and stores; it does not judge.** No alerts, no thresholds, no statistics.
- **`firmware-log-report`** (the analysis): reads the archive, never writes to it. Everything that interprets the logs lives here (section 9). Built after the keeper.

The contract between the two is the archive layout of section 2: the keeper records facts, complete and unmodified, so that any analysis can be done later from the archive alone.

Reference implementation in Python (standard library only) with the test suite; shipped implementation in Rust (one static binary per program). From the same inputs both keepers write byte-identical archive files, delete the same things and exit with the same status; their output is the same but for the wording of errors (section 5). Where a language could decide (numbers, white space, file names, orders), this document gives the rule and both keepers follow it. The one-off import of the legacy layout (section 8) is a command of the Python reference only.

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

Decoder as in `legacy-firmware-log-keeper.py` (`advlog_layout`, `advlog_text`; layout from mu_plus `AdvLoggerPkg/Include/AdvancedLoggerInternal.h`). The header gives the timer frequency and one reading of the firmware clock (year to second) with the timer tick at which it was read; each entry gives a timer tick, a debug level (32-bit) and text. So every line has an uptime (tick × 1000 / frequency, rounded down to the millisecond, as the firmware does when it writes the file log) and a firmware-clock time (the clock reading, plus the line's tick minus the clock's tick, that difference converted and rounded down the same way). An entry's text may span several lines; each line is its own log line with the entry's tick and level. A line built from several entries takes the tick and level of the entry that ends it; a last line without a line break takes those of the last entry. (The legacy decoder used the first entry. The firmware stamps its file log with the last one: on the Surface Book 2 data, 1 to 5 lines per boot fail to pair otherwise.) Text is read as Latin-1.

The dump kept in staging, `advlog.bin`, is the header and the used entries: the bytes from the header's address to the write position. Its sha256 is the memory log's content hash.

The memory log is the only source with no raw copy in the archive, so the bytes of a dump that the decoder does not turn into lines are counted: the bytes skipped to reach the next `ALMS` or `ALM2` signature when what stands at an entry's place is not an entry the decoder knows, and the bytes left before the write position when it finds no further entry. The lines decoded are merged as usual. When that count is not zero, the dump is copied raw into the run folder as `advlog-<its hash>.bin` before its staging or legacy folder is removed, and the keeper run reports one `error: memory:` line giving the count and that file.

### 1.2 File log

- 4 MB, padded after the content with spaces, NUL bytes and line breaks (the Surface Book 2 pads with lines of 70 spaces); CRLF line endings: a line ends at LF, and one CR just before it is not part of the line. The content is the file without its trailing run of space, NUL, CR and LF bytes. A file with no content (empty, or padding only) is skipped with one message: nothing archived, no `seen.tsv` line.
- Each content line is `HH:MM:SS.mmm : <message>` (uptime). The message may be empty, and the space after the colon may then be missing. A blank line is not a log line, but it counts in the line numbers: `file_line` is the number an editor shows.
- A flushed log ends with two blank lines and ` === END_OF_LOG === @ === YYYY/M/D H:MM:SS ===` (firmware clock at the hand-over, to the second). That line is not a log line.
- **Only the content up to and including the first `END_OF_LOG` line belongs to that log.** Anything after it (a stale tail of an older, longer log in the same slot) is not merged; its size (the content bytes after the first `END_OF_LOG` line) is recorded.
- A file without `END_OF_LOG` is incomplete: archived and merged all the same, recorded as incomplete.
- A line that does not parse is kept, with the uptime of the previous line and its raw text. It is in no memory log, so its file is a run of its own (3.1).
- Firmware-clock time of a file line, when its run has no memory log: `END_OF_LOG` clock minus the uptime of the last content line, plus the line's uptime. Without `END_OF_LOG` there is no clock: the time column holds the uptime only (see 3.2).
- An `END_OF_LOG` clock that is not a date and time of the years 1 to 9999 counts as no clock, and so does one from which a row time of the run would leave those years. (The same holds for the clock of a memory log: a line whose time would leave those years has no time, and a run whose reset time would has none.)

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
  runs.tsv                       append-only: one line per run folder made (see below)
  hwerrrec/<sha256 16 hex>.bin   every distinct error record ever seen, raw
  hwerrrec/seen.tsv              append-only (see below)
```

`<run key>`: the Linux boot id (32 hex) for a run whose memory log, timings or versions the keeper collected in that boot, else `fileonly-<sha256 16 hex of the file log content>`: a run known by its file log only. That name states what the keeper has, not what happened: the run may have hung before Linux, or the keeper may have been unable to read the memory log (section 9). The time in the folder name is only a readable, sortable label; identity is the run key. A run with no firmware-clock time (a file log without `END_OF_LOG`, or a Linux boot whose memory log could not be read) takes the UTC time of the keeper run that creates its folder; the folder is renamed if a later run learns the firmware-clock time. A Linux boot gets its folder as soon as the keeper has its memory log, its timings or its versions, even if `firmware.log` then holds the column line only.

- `firmware_bootefilogs/seen.tsv`, one line per file per keeper run: UTC time of the keeper run, Linux boot id, slot (file name), content hash, content length, trailing bytes after the first `END_OF_LOG`, complete (yes/no), `END_OF_LOG` clock as written, value of `UEFI_Index.txt` (the file read as Latin-1, each run of NUL, space, tab, CR and LF bytes replaced by one space, without a space at either end; `-` if nothing is left, if the file is missing or if it cannot be read, which is also reported as `error: bootefi:`).
- `sources.tsv`, one line per source merged into the run: kind (`memory` | `bootefi` | `errrec`), content hash, line count, and for `bootefi` the slot and whether it was the copy merged or an extra matching copy.
- `runs.tsv`, one line each time a run folder is made, written before the folder: UTC time of the keeper run, Linux boot id of the keeper run, run key. It gives the order in which the keeper collected the runs, which the firmware clock in the folder names does not (retention, section 6). The new runs of one keeper run are noted in this order: first the boots of the staging folders, then those of the legacy folders (`migrate`), each group in the order of the folder names, which begin with the UTC time of the keeper run that collected them; then the other runs, by run key compared as text.
- `hwerrrec/seen.tsv`, one line per event: UTC time of the keeper run, Linux boot id, action (`archived` | `deleted` | `kept`), variable name (the file name in efivars; `-` for a record imported by `migrate`), content hash, reason (for `kept`: `archive-mismatch`, `not-verified` or `delete-failed`; else `-`).

Formats, fixed so that two implementations write the same bytes: the `.tsv` files have no column line, their fields are separated by tabs and an absent value is `-`. The UTC time of the keeper run is written `YYYY-MM-DDTHHMMSSZ` everywhere (staging folder name included), with four digits of year. In `firmware_bootefilogs/seen.tsv` the lines of one keeper run are in slot order, and the `END_OF_LOG` clock is copied as the firmware wrote it (`2026/1/2 3:04:05`; tabs as spaces, without the spaces around it). `sources.tsv` has five fields (kind, hash, line count, slot, `merged` or `extra`), with `-` in the last two for `memory` and `errrec` and a line count of 1 for `errrec`; its lines are in the order memory, bootefi (the merged copy, then the extra copies by hash), errrec (by hash). The hash of a memory log migrated from `advlog.txt` alone is the hash of that text, uncompressed. `fpdt-boot.txt` holds one `<file name>=<value>` line per file, sorted by name; `esrt.txt` one line per entry, sorted, `<entry> fw_class=… fw_version=… last_attempt_version=… last_attempt_status=…`. Rules for what the keeper reads back, so that two implementations read it the same way:

- A number (`FWLK_KEEP_RUNS`, `FWLK_KEEP_BYTES`, the line count of `sources.tsv`, the segment and line numbers of `firmware.log`) is ASCII digits only, with nothing around them, and is below 2^63. An uptime of `firmware.log` is an optional minus sign, ASCII digits, a dot and three ASCII digits, and its value in milliseconds is below 2^63 too; a record id (`record_id=0x…` in a row's message) fits in 64 bits. A time of `firmware.log` is exactly `YYYY-MM-DDTHH:MM:SS.mmm` in ASCII digits, a real date and time of the years 1 to 9999. A field that breaks one of these rules is invalid, and so is the file that holds it: a run whose `sources.tsv` or `firmware.log` is invalid keeps its folder as it is and reports `error: merge:`; an invalid `unattached/firmware.log` gets no new row, and no record whose row it should hold is deleted.
- A run folder's name has ASCII digits in its time label. Any other name is not a run folder, and the keeper leaves it alone.
- The `.tsv` files are UTF-8 text; a line ends at LF and nothing else, and an empty line is skipped.
- White space, wherever the keeper removes or collapses it (around the boot id, around a timing or version value, in a printed line), is space, tab, CR and LF, nothing else.
- A file or folder name is compared and sorted by its bytes: staging folders are taken in the order of their names' bytes, and one whose name is not UTF-8 is merged and removed like any other. A timing or version entry whose name is not UTF-8 cannot be written in `fpdt-boot.txt` or `esrt.txt`: its step reports an error and nothing of that source is kept. A setting (section 6) must be UTF-8 text.

### 2.1 No duplicates

- `firmware_bootefilogs/`: one file per distinct content. Seeing the same unrotated file again adds a `seen.tsv` line and nothing else.
- `firmware/`: one folder per firmware run, found by its run key (and, for a file log, by its content hash in the `sources.tsv` files). Re-running the keeper, or seeing the same file again, finds the existing folder. When a `fileonly-` folder turns out to belong to a Linux boot (its memory log is read at a later keeper run), it is merged into that boot's folder and removed, once every error record it held has its row checked in another log; until then it stays and the keeper run reports an error. A `fileonly-` folder whose file log is already listed in a boot's folder (a keeper run could not remove it) is removed the same way by the next keeper run, whether the file is still in `UefiLogs` or not. What such a folder holds is read from its `sources.tsv` and from the error-record rows of its log: if either cannot be read, the folder stays and the keeper run reports an error.
- `firmware/hwerrrec/`: one `.bin` per distinct record (sha256 of the whole variable content). A record that has its row in a log is never added a second time. An archive copy whose row is missing (an earlier run failed in between) gets its row at the next run that sees the record. A row of `unattached/firmware.log` moves to a run's log when a run archived or changed later contains its timestamp (3.1); it leaves `unattached/` only after that run's log has passed its check.

## 3. Merge

### 3.1 Which sources belong to one run

- **Memory log:** a keeper run has the current run's, plus those of staging folders left by a crashed earlier run (4) and, in `migrate`, the legacy ones. Each belongs to the Linux boot it was read in. When that boot's folder already holds a memory log (an earlier keeper run of the same boot, or `migrate` after a keeper run), the lines of the archived one exist nowhere else, so a dump replaces it only if it begins with every archived line: same uptime and text, line by line, where the last archived line may be the unfinished form of its line in the dump (same or later uptime, text that begins with the archived text). A dump that is itself the beginning of the archived log adds nothing and is dropped. Any other dump is kept where it is (staging or legacy folder) and reported as an error; the archived log stays. A dump still in staging (its merge failed, or the keeper run was stopped) is in the same position: a later read of the same boot replaces it there only if it begins with every line of it, by the same rule; a read that is itself the beginning of the staged dump is dropped; any other read is dropped and reported as an error, and the staged dump stays and is merged.
- **File log:** belongs to the run of one of these memory logs only when every one of its row keys (3.3) is among that memory log's row keys. There is no proportion of lines, no clock and no tolerance of any kind: the firmware writes the file from its memory buffer, so the file log of a run is an exact copy of the beginning of that run's memory log (on the Surface Book 2 data the 8 true pairs share all their keys, and a file shares at most 82 % with another boot). The text is compared as it is first; if that comparison is not complete, it is made again without the tags of newer Project Mu firmware (3.3), and that one too must be complete. If several memory logs hold every key of the file: the lowest boot id. Every other file log is another run and gets its own `fileonly-` folder, unless a run folder already lists its content hash. If two files belong to the same memory log, the one with more lines is merged (the lowest content hash if they have as many) and both are listed in `sources.tsv`; the other is an extra copy and adds no row, since each of its lines is already a row of the memory log.
- **Error record:** belongs to the archived run whose firmware-clock span (the earliest to the latest time among its log rows, wherever they are in the log, both included, compared to the second) contains the record's timestamp; if several runs qualify, the one whose span starts latest, then the greatest run key compared as text. A record inside no run goes to `unattached/firmware.log`. This is an exact comparison of two readings of the same clock, with no tolerance. (Records written by a run that hung inside the firmware belong to no archived run: such a run leaves no log.)

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

A new `firmware.log` (a run's, or `unattached/`'s) is written to a temporary file, flushed, read back and checked: every line number of every merged source, and every attached record, appears exactly once with the same uptime and text as in the source (for a file log paired without its tags: the file's text without its tags). Only if the check passes does it replace the log that was there, which is otherwise left untouched: for an older run that log is the only copy of the memory lines. For the same reason a run's new log never replaces one that holds a memory line or an error-record row it lacks, whatever `sources.tsv` lists: the memory rows of the log replaced must be the beginning of the new log's (rule of 3.1) and each of its error records must have its row in the new log; otherwise the old log stays and the keeper run reports `error: merge:`. A run folder found without a `firmware.log` (its first log failed the check, or the keeper run was stopped) gets its log at the next keeper run, from the sources its `sources.tsv` lists. Only then may staging data be removed or a record be deleted from the firmware. A log in which a memory row has lost its uptime (`-`) cannot be compared with a dump: it counts as unreadable, so its run reports `error: merge:` and keeps that log, and the keeper run goes on with the other runs.

## 4. Purges

Every purge happens only after the archive copy has been written, flushed (`fsync`), read back and compared. Each folder made or renamed in the archive is flushed too (`fsync` of its parent folder). A failure keeps the source and is recorded. A folder is removed by removing its files, then each of its subfolders the same way in the order they are listed, then the folder itself; the first failure stops the removal; a symbolic link is removed, never followed, and a folder to remove that is a symbolic link is not removed. A run folder (retention, or a `fileonly-` folder merged into a boot's folder) is removed in two steps, so that a keeper run stopped in between never leaves half a run folder: it is renamed to `<its name>.removing`, the rename is flushed, then it is deleted. A `<run folder name>.removing` folder found by a later keeper run is deleted before the other purges.

1. **Error records:** for each `HwErrRec<nnnn>-414e6bdd-…` whose bytes equal `firmware/hwerrrec/<hash>.bin` exactly and whose row is verified in a `firmware.log`, remove the immutable attribute by running the `chattr` program found in `PATH` with the two arguments `-i <path of the variable>` (a failure of `chattr` itself is ignored) and delete the file in the efivars directory. The path deleted is always the efivars directory plus a name of that exact form. Record it in `hwerrrec/seen.tsv`; if that line (or the `archived` line) cannot be written, the deletion stays done and the keeper run reports `error: errrec:` with the variable's name. Never touch any other variable.
2. **Staging (`firmware_memory/`):** once the memory log, timings and versions are in their run folder and verified, delete that staging folder. A staging folder left by a crashed earlier keeper run is merged and removed by the next one; a dump left under its temporary name `advlog.bin.tmp` is used if it is whole (its bounds fit its length and its first entry sits where the header says), as one more dump of the same boot when `advlog.bin` is there too: each is offered by the rule of 3.1, `advlog.bin` first. A staging folder whose `advlog.bin.tmp` is not whole is kept and reported as an error, whatever else it holds (in the boot it belongs to, the next read of the memory log replaces that file: a cut dump cannot be compared); so is a staging folder with files and nothing readable. A `fileonly-` folder merged into a boot's folder (2.1) is removed before that boot's staging folder.
3. **EFI partition: never.**

## 5. Keeper output

Operational only, to standard output (the journal): what it did and what failed.

- `merged run <key> into <folder> (memory <n> lines, bootefi <n> lines, errrec <n>)`
- `archived file log <hash> from <slot>` / `archived error record <hash> from <variable>` / `deleted error record <variable>`
- `skipped <source>: <reason>`
- `error: <step>: <what>`

In `skipped` and `error` lines the source or step is `memory`, `bootefi`, `errrec`, `fpdt`, `esrt`, or a slot's file name for one file log; in `error` lines it can also be `merge` (writing or checking a run's log), `purge` (removing a folder), `retention`, `migrate` (the old root is refused) or `keeper` (any failure outside a step, a wrong setting included). `<folder>` is the run folder's name; a variable is named by its file name in efivars. A record kept in the firmware (4) is reported with an `error` line naming its variable. An `error` line about a staging or legacy folder that is kept contains that folder's path; one about a run folder that is kept contains its name. Nothing is written to standard error.

In an `error` line only `error: <step>:` is fixed: the text after it is free, and two implementations may word it differently. Every other line is fixed to the byte. A printed line has each run of white space (space, tab, CR, LF) replaced by one space, with none at either end. Output that cannot be written (nobody reads the pipe) is dropped, and the run goes on. A usage error (section 7) prints one line beginning `usage: `, whose text is free.

Exit status 1 if any step raised an error, else 0. Nothing about the content of the logs.

## 6. Configuration

Environment variables (defaults in brackets), so that tests run on scratch directories:

`FWLK_MEMORY_ROOT` [/var/log/firmware_memory], `FWLK_BOOTEFI_ROOT` [/var/log/firmware_bootefilogs], `FWLK_MERGED_ROOT` [/var/log/firmware], `FWLK_EFIVARS` [/sys/firmware/efi/efivars], `FWLK_UEFILOGS` [first existing of /boot/efi/UefiLogs, /efi/UefiLogs, /boot/UefiLogs], `FWLK_DEVMEM` [/dev/mem], `FWLK_SYSFS_FPDT` [/sys/firmware/acpi/fpdt/boot], `FWLK_SYSFS_ESRT` [/sys/firmware/efi/esrt/entries], `FWLK_BOOT_ID` [read from /proc; either way white space around it and dashes are removed and letters put in lower case, and anything else than 32 hex digits is an error], `FWLK_NOW` [current time; for tests, UTC as exactly `YYYY-MM-DDTHH:MM:SSZ` in ASCII digits, a real date and time, else an error], `FWLK_KEEP_RUNS` [300], `FWLK_KEEP_BYTES` [500 MB; a number of bytes], `FWLK_FAULT` [unset; for tests only]. A variable that is set but empty counts as unset. A value that is not UTF-8 text is an error. An error in a setting stops the keeper with one `error: keeper:` line before it reads or writes anything.

`FWLK_FAULT=drop-row` forces the failure that the check of 3.5 exists for: each `firmware.log` written in this keeper run loses its last row in the temporary file, before it is checked. The check must then fail, the log that was there stays, the run reports the error and exits 1, and nothing that depended on that log is purged.

Retention (storage housekeeping, the only policy the keeper has): when `firmware/` holds more than KEEP_RUNS run folders or KEEP_BYTES, the run folders collected first are removed: in the order of the first line of their key in `runs.tsv` (a folder with no line counts as collected before all others, in name order), never the one collected last, and never a run written or checked again in this keeper run, which includes every run whose file log is still in `UefiLogs` (removed, it would come back at the next keeper run as a `fileonly-` folder). A `fileonly-` folder that this keeper run kept, or was merging into a boot's folder (2.1), is not removed by retention either. So the archive can stay above the limits by those runs. The order is not the name order: a boot whose hardware clock reads an old date is still the newest run. Raw file logs no longer referenced by any run folder are removed with them. A run folder's size is the sum of the sizes of the files in it and in its subfolders, added without wrapping (a link to a file counts as that file; a link to a folder is not followed; a folder that cannot be listed counts as empty); a file that cannot be sized stops retention with an error. `hwerrrec/`, `unattached/`, `runs.tsv` and the `seen.tsv` files are never pruned.

The three roots must be three separate folders, none inside another, and none may be the `UefiLogs` folder or the efivars folder, inside one of them or around one of them: otherwise the keeper stops with one `error: keeper:` line before reading or writing anything. Folders are compared after resolving their symbolic links: a path is made absolute and its `.` and `..` are removed as text, then the longest beginning of it that exists is resolved by the system (a beginning that cannot be resolved, through a loop of links, counts as not existing), and the rest is kept as written.

## 7. Keeper commands

```
firmware-log-keeper                       one run (what the unit does)
firmware-log-keeper migrate <old root>    Python reference only: one-off import of the legacy layout (section 8)
```

The shipped keeper has one command, a run. `migrate` is a command of the Python reference only: only a machine that ran the legacy keeper needs it, once, before the first keeper run; to the shipped keeper it is an unknown argument. Any other argument list, an empty `<old root>` included, is a usage error: exit status 2, nothing read or written. `migrate` stops with one `error: migrate:` line and exit status 1, before reading or removing anything, when the old root is not a folder, or is one of the three roots, the `UefiLogs` folder or the efivars folder, holds one of these or is inside one (the legacy root and the merged root have the same default path).

## 8. Migration from the legacy layout (Python reference only)

What `migrate` writes follows sections 2 to 6, so the shipped keeper takes over the archive afterwards. The legacy keeper wrote `/var/log/firmware/<UTC>_<boot id>/` with `advlog.bin[.xz]`, `advlog.txt[.xz]`, `HwErrRec*` copies, `hwerrrec-decoded.txt`, `fpdt-boot.txt`, `esrt.txt`, and `/var/log/firmware/hwerrrec/{<hash>.bin,index.txt}`.

`migrate <old root>`: each legacy folder is a memory log of that Linux boot (decode `advlog.bin`; fall back to `advlog.txt` when the binary is missing), with its `fpdt-boot.txt` and `esrt.txt`. The legacy error-record `.bin` files are imported into `hwerrrec/`, attached to runs by the rule of 3.1, and the legacy `index.txt` is kept as `hwerrrec/legacy-index.txt`. The `HwErrRec<4 hex digits>-414e6bdd-…` copies in the legacy boot folders are imported the same way (the legacy keeper wrote that copy before the one of its archive, so it can be the only one), and a legacy folder is not removed while one of its copies is not archived with its row checked in a log. The file logs currently in `UefiLogs` are archived and paired by content with the imported memory logs. A legacy `.xz` file is read with Python's `lzma` module; a file it refuses keeps its folder, with an error. The compressed files and `advlog.txt` are read in legacy folders only: a keeper run reads neither in a staging folder, and a staging folder that holds nothing else is kept with an error. A legacy folder is removed only after its merged folder is verified, and never when the boot already has a memory log with lines the legacy dump lacks and the dump has lines of its own (3.1). A folder of the old root whose name is that of a run folder is never taken for a legacy folder. The install script moves the legacy root aside first and keeps a tarball of it until the operator deletes it. `migrate` is to be run before the first keeper run: the migrated runs are noted in `runs.tsv` when `migrate` runs, oldest legacy folder first (section 2), so runs the keeper made before it would count as collected before every legacy run.

## 9. `firmware-log-report` (separate program, read-only)

Reads the three roots and prints; writes nothing. Each item is derived from the archive alone:

- list of firmware runs: first time, run key, sources, rows per source, lines found in one copy only, duration of the firmware stage;
- runs known by their file log only (`fileonly-` folders), and error records attached to no run. Such a run never reached Linux only if the keeper could read memory logs then: the keeper run that first saw the file (first line of its hash in `firmware_bootefilogs/seen.tsv`) has a `memory` line in the `sources.tsv` of its own boot's folder. Otherwise the report says only that the memory log was not read;
- rotation gaps: from `firmware_bootefilogs/seen.tsv`, a jump of `UEFI_Index` larger than the number of new files seen means file logs were overwritten before any keeper run saw them;
- long firmware stages and repeated lines, with thresholds given on the command line;
- one global timeline across runs, on request (all rows of all files sorted by `time`);
- `decode-advlog F`: decode a raw memory dump.

Thresholds, wording and output formats of the report can change freely without touching the keeper or the archive.

## 10. Packaging

```
README.md, LICENSE (MIT), SPEC.md, TEST-SCENARIOS.md
python/                   reference implementation (standard library only), with the one-off migrate, and the pytest suite
  tests/                  synthetic fixtures only; the same suite runs against any implementation through FWLK_BIN
                          (the tests of migrate run against the reference only)
  tests/test_realdata.py  optional, runs only when FWLK_REALDATA points to a local snapshot
rust/                     Cargo workspace: firmware-log-keeper (one command, a run), firmware-log-report, shared library crate
systemd/firmware-log-keeper.service
install.sh                install | undo | status (root)
```

The shipped keeper is the Rust one and has one command; a machine that ran the legacy keeper runs the Python reference's `migrate` once, before the unit's first run. Nothing in the code is specific to one machine: no addresses, slot counts or device names. The unit:

```
[Unit]
Description=Keep the firmware's own logs (Advanced Logger, error records, boot timings, versions)
After=local-fs.target
RequiresMountsFor=/var/log
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/firmware-log-keeper
[Install]
WantedBy=multi-user.target
```
