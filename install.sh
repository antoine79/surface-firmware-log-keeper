#!/bin/bash
# Install, check or remove the Surface firmware log keeper. Run as root from the project folder, after building
# (or from the folder of a release download, which holds the programs ready-built in bin/):
#   (cd rust && cargo build --release)
#   sudo ./install.sh install | status | undo | file-logs
# install  copies the keeper to /usr/local/sbin, the report to /usr/local/bin and the unit to /etc/systemd/system,
#          enables the unit and runs it once
# status   unit state, this boot's keeper output, what is left in staging and in the firmware's record store
# undo     disables and removes the unit and both programs; the archive under /var/log is kept
# file-logs  turns on the firmware's own log files: creates the empty folder UefiLogs on the EFI partition, which
#          Surface firmware writes to only if it exists (9 files of up to 4 MB). Undo: remove that folder
set -euo pipefail
cd "$(dirname "$0")"
BIN=/usr/local/sbin/firmware-log-keeper
UNIT=/etc/systemd/system/firmware-log-keeper.service
# the programs: in bin/ of a release download, else the local build
BUILT=$( [ -x bin/firmware-log-keeper ] && echo bin || echo rust/target/release )
REPORT=/usr/local/bin/firmware-log-report

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

# the EFI partition: the first of these mounted as FAT
esp() { for d in /boot/efi /efi /boot; do [ "$(findmnt -n -o FSTYPE --mountpoint "$d" 2>/dev/null)" = vfat ] && { echo "$d"; return; }; done; }

case "${1:-}" in
install)
	[ -x "$BUILT/firmware-log-keeper" ] && [ -x "$BUILT/firmware-log-report" ] || { echo "build first: (cd rust && cargo build --release)"; exit 1; }
	[ -d /sys/firmware/efi ] || { echo "not booted in UEFI mode: nothing to keep"; exit 1; }
	install -m 755 "$BUILT/firmware-log-keeper" "$BIN"
	install -m 755 "$BUILT/firmware-log-report" "$REPORT"
	install -m 644 systemd/firmware-log-keeper.service "$UNIT"
	systemctl daemon-reload
	systemctl enable firmware-log-keeper.service
	systemctl start firmware-log-keeper.service || true
	"$0" status
	;;
status)
	echo "unit: $(systemctl is-enabled firmware-log-keeper.service 2>/dev/null || true)," \
	     "last result: $(systemctl show -p Result --value firmware-log-keeper.service)"
	journalctl -u firmware-log-keeper.service -b --no-pager -o cat | tail -20
	echo "staging entries (0 after a good run): $(find /var/log/firmware_memory -mindepth 1 2>/dev/null | wc -l)"
	echo "error records still in the firmware: $(ls /sys/firmware/efi/efivars | grep -c '^HwErrRec[0-9A-F]\{4\}-' || true)"
	e=$(esp)
	if [ -z "$e" ]; then echo "file logs: no EFI partition mounted"
	elif [ -d "$e/UefiLogs" ]; then echo "file logs: on ($e/UefiLogs, $(ls "$e/UefiLogs" | grep -c '^UEFI_Log') files)"
	else echo "file logs: off (sudo ./install.sh file-logs turns them on)"; fi
	echo "archive size: $(du -sch /var/log/firmware /var/log/firmware_bootefilogs 2>/dev/null | tail -1 | cut -f1)"
	;;
undo)
	systemctl disable firmware-log-keeper.service 2>/dev/null || true
	rm -f "$UNIT" "$BIN" "$REPORT"
	systemctl daemon-reload
	echo "removed $UNIT, $BIN and $REPORT; the archive in /var/log/firmware* is kept"
	;;
file-logs)
	e=$(esp)
	[ -n "$e" ] || { echo "no EFI partition mounted at /boot/efi, /efi or /boot"; exit 1; }
	[ -d "$e/UefiLogs" ] && { echo "file logs already on: $e/UefiLogs exists"; exit 0; }
	free=$(df -m --output=avail "$e" | tail -1 | tr -d ' ')
	[ "$free" -ge 40 ] || { echo "only $free MB free on $e: the firmware needs up to 37 MB"; exit 1; }
	mkdir "$e/UefiLogs"
	echo "created $e/UefiLogs: the firmware writes its log there from the next boot on (undo: rm -r $e/UefiLogs)"
	;;
*)
	sed -n '2,10p' "$0"; exit 1
	;;
esac
