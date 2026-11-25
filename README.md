# firmware-log-keeper

Keeps the logs that Project Mu UEFI firmware (Microsoft Surface and other Mu-based machines) leaves behind, which no Linux distribution saves today. It runs once per Linux boot as root, copies what the firmware left, merges it into one log per firmware run, and frees the firmware's error-record storage once each record is safely archived. It collects and stores; it never judges the content of the logs.

[SPEC.md](SPEC.md) is the full specification; [TEST-SCENARIOS.md](TEST-SCENARIOS.md) lists what the tests check.

## What it reads

- the memory log of the current boot (the Advanced Logger buffer in RAM, read through `/dev/mem`);
- the file logs `UEFI_Log1.txt` to `UEFI_Log9.txt` in the `UefiLogs` folder of the EFI partition, which it only ever reads;
- the error records, UEFI variables `HwErrRec<nnnn>-414e6bdd-e47b-47cc-b244-bb61020cf516`;
- the boot timings (`/sys/firmware/acpi/fpdt/boot`) and firmware versions (`/sys/firmware/efi/esrt/entries`).

A missing source is skipped and the others are still processed.

## What it writes, and where

```
/var/log/firmware_memory/        staging: this boot's memory log, timings and versions, removed once merged
/var/log/firmware_bootefilogs/   every distinct file log ever seen, raw, and seen.tsv
/var/log/firmware/
  <time>_<run key>/              one folder per firmware run: firmware.log, sources.tsv, fpdt-boot.txt, esrt.txt
  unattached/firmware.log        error records that fall inside no archived run
  hwerrrec/                      every distinct error record ever seen, raw, and seen.tsv
```

It deletes only two things: a staging folder once its content is in a run folder and checked, and an error-record variable once its raw copy is written, read back and equal and its row is checked in a log. Nothing on the EFI partition is ever written. Every root can be moved with an `FWLK_*` variable (SPEC section 6). Its output goes to standard output, one line per thing done or failed; the exit status is 1 if anything failed.

## Report

`firmware-log-report` reads the archive and prints a summary; it writes nothing and needs no root. It lists every firmware run (sources, rows, time to the hand-over to the boot loader), the runs known only by their file log (a hang in the firmware, or another system started), every error record with when it was archived and deleted, file logs the firmware overwrote before any keeper run saw them, firmware stages over 30 s and lines repeated more than 50 times in one run (`--stage-over SECONDS`, `--repeat-over COUNT`). `firmware-log-report timeline` prints every row of the archive sorted by firmware-clock time, and `firmware-log-report decode-advlog FILE` decodes a raw memory dump. SPEC section 8 has the details.

## Build

```
cd rust
cargo build --release --offline
cargo test --offline
```

The binaries are `rust/target/release/firmware-log-keeper` and `rust/target/release/firmware-log-report`. It needs only the standard library and the `sha2` crate, and has no unsafe code.

## Test

The tests are black-box: they run the keeper on scratch folders and judge the files it writes.

```
FWLK_BIN=$PWD/rust/target/release/firmware-log-keeper \
  uv run --python 3.13 --with pytest pytest -q -p no:cacheprovider tests
```

The report tests run the `firmware-log-report` built beside the keeper, or the one named by `FWLK_REPORT_BIN`. `FWLK_REALDATA=<folder>` adds a run on a copy of a real archive (folders `firmware/`, `firmware_bootefilogs/`, `UefiLogs/`, `efivars/`); the folder itself is never written. `FWLK_PARITY_BIN=<another build>` runs every scenario a second time with that build and compares the two.

## Install

```
(cd rust && cargo build --release --offline)
sudo ./install.sh install
```

This copies the keeper to `/usr/local/sbin/firmware-log-keeper`, the report to `/usr/local/bin/firmware-log-report` and the unit `systemd/firmware-log-keeper.service` to `/etc/systemd/system/`, enables it and runs it once. `sudo ./install.sh status` shows what the last run did; `sudo ./install.sh undo` removes the unit and both programs and keeps the archive under `/var/log`.

The keeper deletes the firmware's error records once they are archived (see above): that is how the firmware's small record store (11 slots on a Surface Book 2) gets room for new ones. If you want the records left in the firmware, do not install it.

## Tested on

A Surface Book 2 13" (firmware from Microsoft's Project Mu builds) under Fedora 45 with the linux-surface kernel, at every boot since 21 Nov 2026. Nothing in the code is specific to that machine: it finds the memory log through the `AdvLoggerLocator` UEFI variable, and the error records, boot timings and versions are standard UEFI and ACPI. Other Mu-based machines are expected to work but have not been checked; reports are welcome.

## License

MIT, see [LICENSE](LICENSE).
