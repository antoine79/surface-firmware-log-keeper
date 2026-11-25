# Design

Read this note before the code: it covers how the code is laid out and why. [SPEC.md](SPEC.md) is the full contract.

## Two programs and a contract

The keeper collects, the report interprets, and the only thing they share is the archive layout of SPEC section 2.

- `firmware-log-keeper` runs once per boot as root. It copies what the firmware left, merges it into one log per firmware run and frees the firmware's error-record store. It never judges the content.
- `firmware-log-report` reads the archive and prints. It writes nothing and needs no root.

The archive is plain text: tab-separated logs and lists, plus raw copies named after their content. Any tool can read it, and either program can be rewritten without touching the data. The keeper already was: it began in Python, and the Rust version replaced the Python reference on 21 Nov 2026 once both wrote byte-identical archives on the whole test suite.

## A keeper run

The keeper's `main.rs` runs four steps in order. The first only copies, the second writes nothing, and only the last one deletes.

1. **Collect** (`step1_collect.rs`). Copy this boot's memory log, boot timings and firmware versions to a staging folder, and archive each file log and error record raw. A source that fails never stops the others.
2. **Plan** (`step2_plan.rs`). From the archive as read at the start of the run (`archive_reader.rs`), decide which firmware run each source belongs to and build the files of every run folder concerned. Each new log is checked here, in memory, before it is written.
3. **Write** (`step3_write.rs`). Write the run folders that changed: `sources.tsv` first, so that a run stopped halfway finds what the folder should hold, and `firmware.log` last.
4. **Purge** (`step4_purge.rs`). Remove each staging folder whose content is now in a checked log, and delete from the firmware each error record whose row is.

All the decisions live in the plan, so writing and deleting stay short and simple.

## Where things are

The code is in the library `rust/firmware_logs`; each program is one `main.rs` that uses it.

| File | What it does |
|---|---|
| `archive_reader.rs` | reads the archive as found at the start of a run |
| `dates.rs` | dates and times to the millisecond, since the standard library has no calendar |
| `firmware_error_record.rs` | decodes an error record the firmware stores, in the UEFI Common Platform Error Record format |
| `firmware_log_file.rs` | reads one `UEFI_Log<n>.txt` the firmware writes on the EFI partition |
| `firmware_memory_log.rs` | reads the log the firmware keeps in memory (Project Mu's Advanced Logger buffer), through `/dev/mem` or the firmware's log variables |
| `firmware_timings_and_versions.rs` | copies the boot timings and firmware versions Linux shows in `/sys` |
| `log_format.rs` | writes and reads back the two files of a run folder, `firmware.log` and `sources.tsv` |
| `merge_logs.rs` | the key of each log line, the pairing of a file log with a memory log, the rows of one run, and the checks before any purge |
| `output.rs` | the lines the keeper prints |
| `safe_files.rs` | reads that tell a missing file from a failure, and writes that are flushed, read back and compared |
| `settings.rs` | the `FWLK_*` settings |
| `step1_collect.rs` to `step4_purge.rs` | the four steps above |
| `firmware-log-keeper/src/main.rs` | the keeper: runs the four steps |
| `firmware-log-report/src/main.rs` | the report |

The report reads the archive with `log_format.rs`, `archive_reader.rs` and `dates.rs`, and decodes dumps with `firmware_memory_log.rs`, so the two programs cannot disagree on a format. The modules are public so that both programs can use them; they are not a stable interface.

## Design decisions

**Nothing is deleted before its copy is proven.** A file is written under a temporary name, flushed to disk, read back, compared, then renamed. A staging folder is removed, and an error record deleted from the firmware, only once the log holding it has passed that check. After that, the archive holds the only copy: the memory log is gone at the next reset, and a deleted record is gone from the firmware. The records must be deleted all the same, because the firmware's error store (11 slots on the Surface Book 2) never frees a slot by itself and drops new errors once full.

**Nothing is written on the EFI partition.** Windows and the firmware use it too. Its files are opened with `O_NOATIME`, which root may always use, because updating a file's access time is itself a write.

**Pairing is exact.** A file log belongs to a memory log only if every one of its lines is in that memory log, each line keyed by its segment, uptime, text and occurrence (SPEC 3.3). The firmware writes the file from its memory buffer: on the Surface Book 2 data the 8 true pairs share all their lines and no other pair more than 82 %, so an exact match separates them with no threshold to tune.

**Raw copies are named after their content.** The first 16 hex digits of the SHA-256 hash name each file log and error record. Seeing the same file again finds the copy already there and adds nothing.

**Output is deterministic.** Rows are sorted on a total order (SPEC 3.4), so the same sources always give the same bytes. A file that already holds those bytes is not rewritten, and a second keeper run in the same boot changes nothing.

**Firmware times are stored as found.** The keeper copies the firmware clock without converting it. This laptop's hardware clock moved from Tokyo time to UTC on 20 Nov 2026, and each log still agrees with its own error records.

**Folder names never change.** A run folder is named after its first firmware time (or, without one, the time of the keeper run) and its run key: the Linux boot id, or `fileonly-` and the hash of its file log. The key is the identity; the time only sorts the folders.

**The standard library, one dependency, no unsafe code.** The one dependency is `sha2`. The kernel marks most UEFI variables immutable, and clearing that flag needs a system call the standard library does not offer, so the keeper runs `chattr -i` before deleting a record.

**Two ways to the memory log.** `/dev/mem` comes first: it is what the keeper was proven on. A kernel in lockdown mode, which some distributions turn on with Secure Boot, refuses it, and the memory log is what ties a file log to its Linux boot: without it every run would look like one that never reached Linux. So the keeper then reads the same bytes through variables the firmware offers for this, which lockdown does not block (SPEC 1.1).

**Settings are environment variables only.** They let the tests point every path at a scratch folder; a real machine needs none.

**The keeper prints facts, one per line, and raises no alert.** Its output goes to the system journal. Interpreting the logs is the report's job.

## Tests

The tests are black-box, so they do not depend on how the code is written. Python (pytest) builds a fake machine in a scratch folder (memory log, EFI partition, UEFI variables), runs a program and judges the files it writes. That made the rewrite safe: `FWLK_PARITY_BIN` runs every scenario on two builds and compares them. After each scenario the suite also checks the standing rules, among them no write on the EFI partition and no variable deleted unless archived. [TEST-SCENARIOS.md](TEST-SCENARIOS.md) lists the scenarios by group.

## Known limits

- **Checked on one machine**, a Surface Book 2 13" under Fedora.
- **The timeline follows the firmware clock**, so changing the clock reorders runs around the change.
