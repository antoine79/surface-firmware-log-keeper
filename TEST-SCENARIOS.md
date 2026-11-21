# Test scenarios

Vocabulary: a firmware run is one execution of the firmware from reset to hand-over (or hang); see SPEC.md.

Each scenario is a sequence of simulated boots on scratch directories: fake firmware memory, fake `UefiLogs` folder, fake error-record variables. The keeper runs once per simulated Linux boot. Every scenario also checks the standing rules at the end (section H).

## A. One boot, one or both sources

- A1. Memory only (no `UefiLogs` folder): one boot folder, every row `memory`.
- A2. Both copies identical: every row `both`.
- A3. Memory has extra lines after the hand-over: those rows are `memory`, at the end.
- A4. One line differs between the copies: not every line of the file is in the memory log, so the file is another run, with its own `fileonly-` folder (every row `bootefi`), and the boot's folder holds its memory lines only (every row `memory`). No warning: the keeper does not judge.
- A5. A memory entry spanning several lines: each line paired with the file's lines.
- A6. Row key: several lines in the same millisecond, lines with an empty message, and lines identical in time and text (occurrence 1, 2, 3): memory has such a line three times and the file twice gives two `both` rows and one `memory` row. Only spaces, tabs, CR and LF are removed at the end of a message: a form feed or a non-breaking space stays.
- A12. A memory dump with an entry the decoder does not know: the other lines are merged, the dump is copied raw into the run folder before staging is emptied, and the run reports the bytes not decoded.
- A7. File only, for a boot that never reached Linux: a `fileonly-` folder with every row `bootefi`. Nothing is flagged by the keeper.
- A9. Segment safeguard: a source whose uptime goes backwards starts segment 1; rows sort by segment first, so two runs in one log are never interleaved.
- A10. Every row has a firmware-clock time: from the memory header for a run with a memory log, derived from `END_OF_LOG` for a file-only run, `-` when the file has no `END_OF_LOG`. With a clock tick between two milliseconds, the reset time is still the first memory row's time minus its uptime, and an error record's uptime is counted from it.
- A11. File log with the tags of newer Project Mu firmware (phase and level, or level only): paired with the memory log without the tags, every row `both` with the memory copy's text; a file-only run keeps every text as it is. A line without level tag whose text begins like one loses that beginning too: its file is then another run.
- A8. Sort order: rows are sorted by segment, time since reset, then source, then line number (a `both` row, error records and a `memory` row at the same uptime); running the merge twice on the same inputs gives a byte-identical file; every row carries its line number in its source.

## B. Several boots

- B1. Linux boot, then a boot that hangs after the firmware, then a Linux boot: three folders; the middle one file-only.
- B2. The same 9 unrotated files seen at 9 runs in a row: each archived once, no new folder; `seen.tsv` gains one line per file per run.
- B3. Rotation: a slot overwritten by a new boot; the old content stays archived, the new one is added.
- B4. Two files whose every line is in the current memory log: one folder, the longer file merged, both listed in its sources. If they are as long, the lowest content hash is merged. A file whose every line is in two memory logs of one keeper run goes to the lowest boot id, as an extra copy there if that boot has its own file: the first keeper run and a second one give the same archive.
- B5. Two different boots whose first lines happen to be identical: told apart by the rest of their content, two folders.
- B5b. A file from another boot whose power-on time is close to the current boot's: still a separate boot (the clock never joins two sources).
- B5c. Two boots sharing 80 % of their lines, the older one known by its file only: two runs, because not every line of the file is in the other boot's memory log. With or without `END_OF_LOG`, the result is the same.
- B6. The keeper run twice in the same boot: nothing new the second time. If the second run reads a shorter memory log, every earlier row stays and nothing is reported; a memory log with other lines is kept in staging and reported, the archived log stays; a longer one (the firmware went on logging, an unfinished line now ended) replaces the first. A dump still in staging because its merge failed is protected the same way from a later read of the same boot.
- B7. A long run of boots that never reached Linux (more than 9): the keeper archives what is there and records the firmware's index value in `seen.tsv`; the gap is found by the report program (K2), not by the keeper.

## C. Damaged or unusual file logs

- C1. Stale tail after `END_OF_LOG` (an older, longer log in the same slot): only the part before the first marker is merged; the tail is kept in the raw copy and reported.
- C2. No `END_OF_LOG` (interrupted write): archived and merged, flagged as incomplete. A last line cut inside its time stamp is in no memory log: the file is another run.
- C3. Padding with spaces, with NUL bytes, or both; Windows line endings.
- C4. Empty file, or a file of only padding: ignored, reported.
- C5. Unreadable or garbled lines: kept as they are, never stop the run; being in no memory log, they make their file a run of its own. A line ended by LF alone is a line. An `END_OF_LOG` clock outside the years 1 to 9999 counts as no clock.

## D. Error records

- D1. New records: archived once by content, decoded in the boot folder, reported.
- D2. Purge: each record whose archived copy matches byte for byte is deleted from the firmware; the store is empty afterwards.
- D3. A record whose archive copy differs: kept in the firmware, reported.
- D4. Never deleted: `HwErrRecSupport`, the logger's locator variable, a variable with a similar name but another identifier, any other variable.
- D5. Removing the write protection (`chattr`) fails: deletion still attempted, failure reported if it fails. A variable that can only be deleted after `chattr -i <path>` has run is deleted.
- D6. The same record seen again later (re-written by the firmware): not archived twice.
- D7. A record that cannot be decoded: archived raw, reported, not deleted only if the raw archive cannot be verified.

- D8. A record whose timestamp falls inside the current run's clock span: one row with source `errrec` in that run's log, at its time; listed in `sources.tsv`.
- D9. A record inside an older archived run's span (found one boot later): added to that run's log, which is rewritten in order.
- D10. A record inside no run (written by a run that hung in the firmware and left no log): one row in `unattached/firmware.log`. It stays there when its run is archived at a later keeper run.
- D11. A record exactly on the first or last second of a run: attached (bounds included). A record one second outside: unattached. A record of the first second, before a reset time that has a millisecond part: attached with a negative uptime, first row. In a log with two segments the span goes from the earliest to the latest row time.
- D12. A record is deleted from the firmware only after its row is verified in a log; the same record seen again is not added twice.
- D13. Two runs whose clock spans overlap (clock set back between them): the record goes to the run with the latest first row. Two spans that start on the same millisecond: the greatest run key, compared as text.
- D14. Two records with the same second and the same record id in one run: in hash order, whatever the order of their variables.
- D15. A line of `hwerrrec/seen.tsv` that cannot be written: the record is archived and deleted all the same, the run reports the error and exits 1.

## E. Safety of every delete

- E1. The archive write fails (disk full, read-only `/var/log`): nothing is deleted anywhere, the run reports the error and exits 1.
- E2. Crash between writing the merged log and purging: the next run finds the staging data, finishes the merge, then purges. A file left under its temporary name is ignored and goes with its folder. A staging folder whose dump cannot be decoded, or with a file the keeper does not know and nothing readable, is kept and reported.
- E3. The merged log is missing a line (forced): the check before purging fails, the staging data stays. The log that was there before (an older run's, or `unattached/`'s) is byte for byte unchanged, and the next run without the fault completes, also for a run folder left without a log whose file is no longer in `UefiLogs`. A log is never replaced by one that lacks an error-record row or a memory line it holds, even when `sources.tsv` no longer lists them. A log damaged from outside so that a memory row has lost its uptime stops only its own run: that run reports `error: merge:` and keeps its log, and the keeper run goes on with the other sources.
- E5. Two of the three roots equal or one inside another, or a root that is the `UefiLogs` folder or the efivars folder, inside one or around one: the keeper stops with one error, nothing read or written.

## F. Missing sources and errors

- F1. No firmware memory log (no locator variable, or `/dev/mem` not readable): skipped with one message, the other sources processed. When the memory log is readable at a later run, the `fileonly-` folder keeps its file log and records, and the boot's folder, under its first name, gets the memory lines.
- F2. No `UefiLogs` folder, or no EFI partition mounted: skipped with one message. An unreadable `UEFI_Index.txt`: reported, the file logs archived with `-` as index, the other sources processed.
- F3. No error records, no boot timings, no firmware versions: skipped quietly or with one message each.
- F4. One step raises an error: the other steps still run, exit status 1.
- F6. A boot id given with dashes or capitals names the same folders; one that is not 32 hex digits is an error, nothing written.
- F5. A firmware stage of two hours with thousands of repeated lines: merged like any other, no warning from the keeper.

## H. Standing rules, checked after every scenario

- H1. The `UefiLogs` folder is byte-for-byte unchanged (hash of the whole tree before and after), and no access time of its files is updated.
- H2. No variable other than archived `HwErrRec<nnnn>` records was deleted.
- H3. Every line of every source appears in the merged log exactly once. A run with a memory log has `both`, `memory` and `errrec` rows only; a file-only run has `bootefi` and `errrec` rows only.
- H4. No duplicate boot folders, no duplicate archive files.
- H5. The staging folder is empty after a successful run.
- H6. The keeper's output contains only what it did and what failed: no warning about the content of the logs.

Regression mode: with `FWLK_PARITY_BIN` naming another build, every keeper call is made again by it on a copy of the scratch tree, and both must give the same exit status, files and output (error texts aside).

## I. Commands

- I1. The keeper has one command, a run. Any argument is a usage error (exit status 2, nothing read or written). Decoding and status belong to the report program (K).
- I2. Without `FWLK_BOOT_ID` and `FWLK_NOW`: the kernel's boot id without dashes, the current time in UTC whatever the time zone. A variable that is set but empty counts as unset.

## L. Damaged or hostile input

The keeper never panics; a failure gives an error line naming its step and exit status 1, and nothing is deleted that should not be.

- L1. A `sources.tsv` whose line count is not a number stops its run with `error: merge:`, the folder unchanged. A damaged `unattached/firmware.log` keeps the record of its row in the firmware (`error: errrec:`). `FWLK_NOW` in another format, or a setting that is not UTF-8: `error: keeper:`, nothing written.
- L2. A staging folder that cannot be removed: `error: purge:`. A slot that is a folder is named in its error line, the EFI partition unchanged. Two staging folders of one boot with other timings: the run takes the later ones, and the earlier folder stays with an `error: memory:` line naming it. A merged root that cannot be listed: `error: keeper:`, nothing merged or purged, the file logs archived all the same.
- L3. With standard output on a pipe nobody reads, a run does its work and exits as usual, and nothing goes to standard error; so does a usage error.

## K. Report program (read-only, separate)

- K1. Lists the firmware boots with their sources, line counts and firmware duration, from the archive alone.
- K2. Rotation gap: detected from the index values in `seen.tsv` (B7's archive).
- K3. Boots that never reached Linux listed; long firmware stages listed above a threshold given on the command line.
- K4. Error records decoded, with when each was archived and deleted.
- K5. The archive is byte-for-byte unchanged after any report command.

## J. Real data (local only, never in the repository)

- J1. On a copy of the archive the installed keeper wrote on this laptop, with its `UefiLogs` and efivars, and no memory log: two runs, both exit 0; no `firmware.log` changes by one byte; the error records already archived are deleted from the copy of efivars, and only they; `UefiLogs` unchanged, access times included; staging empty; the second run adds only `seen.tsv` lines.
