#!/bin/bash
# Install, check or remove the firmware-log keeper. Run as root from the project folder, after building:
#   (cd rust && cargo build --release --offline)
#   sudo ./install.sh install | status | undo
# install  copies the keeper to /usr/local/sbin, the report to /usr/local/bin and the unit to /etc/systemd/system,
#          enables the unit and runs it once
# status   unit state, this boot's keeper output, what is left in staging and in the firmware's record store
# undo     disables and removes the unit and both programs; the archive under /var/log is kept
set -euo pipefail
cd "$(dirname "$0")"
BIN=/usr/local/sbin/firmware-log-keeper
UNIT=/etc/systemd/system/firmware-log-keeper.service
BUILT=rust/target/release/firmware-log-keeper
REPORT=/usr/local/bin/firmware-log-report

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

case "${1:-}" in
install)
	[ -x "$BUILT" ] && [ -x rust/target/release/firmware-log-report ] || { echo "build first: (cd rust && cargo build --release --offline)"; exit 1; }
	[ -d /sys/firmware/efi ] || { echo "not booted in UEFI mode: nothing to keep"; exit 1; }
	install -m 755 "$BUILT" "$BIN"
	install -m 755 rust/target/release/firmware-log-report "$REPORT"
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
	echo "archive size: $(du -sch /var/log/firmware /var/log/firmware_bootefilogs 2>/dev/null | tail -1 | cut -f1)"
	;;
undo)
	systemctl disable firmware-log-keeper.service 2>/dev/null || true
	rm -f "$UNIT" "$BIN" "$REPORT"
	systemctl daemon-reload
	echo "removed $UNIT, $BIN and $REPORT; the archive in /var/log/firmware* is kept"
	;;
*)
	sed -n '2,8p' "$0"; exit 1
	;;
esac
