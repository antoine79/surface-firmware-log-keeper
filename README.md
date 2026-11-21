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

## Build

```
cd rust
cargo build --release --offline
cargo test --offline
```

The binary is `rust/target/release/firmware-log-keeper`. It needs only the standard library and the `sha2` crate, and has no unsafe code.

## Test

The tests are black-box: they run the keeper on scratch folders and judge the files it writes.

```
FWLK_BIN=$PWD/rust/target/release/firmware-log-keeper \
  uv run --python 3.13 --with pytest pytest -q -p no:cacheprovider tests
```

`FWLK_REALDATA=<folder>` adds a run on a copy of a real archive (folders `firmware/`, `firmware_bootefilogs/`, `UefiLogs/`, `efivars/`); the folder itself is never written. `FWLK_PARITY_BIN=<another build>` runs every scenario a second time with that build and compares the two.

## Install

```
sudo install -m755 rust/target/release/firmware-log-keeper /usr/local/sbin/firmware-log-keeper
```

Then add the systemd unit given in SPEC section 9 as `/etc/systemd/system/firmware-log-keeper.service` and run `sudo systemctl enable firmware-log-keeper.service`. To undo: `sudo systemctl disable firmware-log-keeper.service`, then remove the unit and the binary. The archive under `/var/log` stays.
