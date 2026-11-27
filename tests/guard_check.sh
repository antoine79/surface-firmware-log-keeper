#!/bin/bash
# Check the systemd guard of systemd/firmware-log-keeper.service on a real machine. Run as root from the project
# folder, after building: sudo tests/guard_check.sh
#  1. A probe runs inside the same guard and checks what it can and cannot reach: writes, devices, privileges,
#     network, and deleting a UEFI variable the way the keeper deletes an archived error record (a test variable
#     this script creates and the probe removes).
#  2. The keeper runs inside the guard into a scratch archive (/var/log/fwlk-guard-test, made by systemd as on a
#     first install) and must give this boot's run folder byte for byte as the real archive holds it: once reading
#     /dev/mem, once reading the firmware's log variables.
# The real archive is only compared with, and the probe's test files in it are removed at once. The test units live
# in /run/systemd/system and are removed on exit with the scratch archive. Refuses to run while an error record is
# waiting in the firmware: the scratch run would take it away from the real archive.
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
UNIT=${UNIT:-systemd/firmware-log-keeper.service}  # another unit file to check, for testing this script
KEEPER=/usr/local/sbin/firmware-log-keeper
EFIVARS=/sys/firmware/efi/efivars
T=/var/log/fwlk-guard-test
VAR=$EFIVARS/FwlkGuardProbe-$(cat /proc/sys/kernel/random/uuid)
PROBE=/run/fwlk-guard-probe.sh
BOOT=$(tr -d '-' < /proc/sys/kernel/random/boot_id)
fails=0

ls "$EFIVARS" | grep -q '^HwErrRec[0-9A-F]\{4\}-' && { echo "an error record is waiting in the firmware: let the keeper archive it first"; exit 1; }
BUILT=$( [ -x bin/firmware-log-keeper ] && echo bin || echo rust/target/release )  # as install.sh finds it
cmp -s "$BUILT/firmware-log-keeper" "$KEEPER" || { echo "$KEEPER is not this build: sudo ./install.sh install first"; exit 1; }
REAL=$(ls -d /var/log/firmware/*_"$BOOT" 2>/dev/null) || { echo "no run folder for this boot in /var/log/firmware"; exit 1; }
ESP=$(for d in /boot/efi /efi /boot; do [ -d "$d/UefiLogs" ] && { echo "$d"; break; }; done)
[ -n "$ESP" ] || { echo "no UefiLogs folder on an EFI partition"; exit 1; }
DISK=/dev/$(lsblk -no PKNAME "$(findmnt -nvo SOURCE /)" | head -1)
[ -b "$DISK" ] && head -c 1 "$DISK" > /dev/null || { echo "cannot find or read the disk of / ($DISK)"; exit 1; }

cleanup() {
	rm -f /run/systemd/system/fwlk-guard-*.service /run/fwlk-guard-*.out "$PROBE"
	systemctl daemon-reload
	rm -rf "$T"
	if [ -e "$VAR" ]; then chattr -i "$VAR" 2>/dev/null || true; rm -f "$VAR"; fi
}
trap cleanup EXIT
set +e  # from here on a failed check is counted, not fatal

# A copy of the unit, without [Install], with another ExecStart and lines added at the end of [Service].
unit() {
	local name=$1 exec=$2; shift 2
	sed -e "s|^ExecStart=.*|ExecStart=$exec|" -e '/^\[Install\]/,$d' "$UNIT" > "/run/systemd/system/$name.service"
	printf '%s\n' "$@" "StandardOutput=truncate:/run/$name.out" >> "/run/systemd/system/$name.service"
}
run() {  # start a test unit and wait for it; print its output (a file, so no line can be missed); its exit status
	systemctl daemon-reload
	local rc=0
	systemctl start "$1.service" || rc=$?
	cat "/run/$1.out"
	return $rc
}
check() { if [ "$1" = 0 ]; then echo "ok    $2"; else echo "FAIL  $2"; fails=$((fails + 1)); fi; }

echo "== 1. what the guard lets through"
printf '\x07\x00\x00\x00fwlk' > "$VAR"  # non-volatile, boot and runtime access; 4 bytes of data
chattr +i "$VAR"                         # as the kernel marks most variables
cat > "$PROBE" <<'EOF'
say() { if [ "$1" = 0 ]; then echo "ok    $2"; else echo "FAIL  $2"; fi; }
can_write() { touch "$1/.fwlk-guard-probe" 2>/dev/null && rm -f "$1/.fwlk-guard-probe"; }
for d in /var/log/firmware /var/log/firmware_memory /var/log/firmware_bootefilogs; do
	can_write "$d"; say $? "can write $d"
done
for d in "$ESP" "$ESP/UefiLogs" /etc /usr/local/sbin /var/log /var/lib /root /home; do
	can_write "$d"; [ $? != 0 ]; say $? "cannot write $d"
done
ls "$ESP/UefiLogs" > /dev/null; say $? "can read $ESP/UefiLogs"
findmnt -n -o OPTIONS -T /sys | grep -q '^ro'; say $? "/sys is read-only"
findmnt -n -o OPTIONS -T "$EFIVARS" | grep -q '^rw'; say $? "$EFIVARS is writable"
head -c 1 /dev/mem > /dev/null; say $? "can read /dev/mem"
head -c 1 "$DISK" > /dev/null 2>&1; [ $? != 0 ]; say $? "cannot read $DISK"
[ "$(awk '/^CapEff/ {print $2}' /proc/self/status)" = 0000000000020200 ]; say $? "privileges: raw memory and immutable flag only"
[ "$(tail -n +3 /proc/net/dev | grep -vc '^ *lo:')" = 0 ]; say $? "no network but loopback"
chattr -i "$VAR" && rm "$VAR"; say $? "can delete a UEFI variable as the keeper does"
EOF
unit fwlk-guard-probe "/bin/bash $PROBE" "Environment=ESP=$ESP EFIVARS=$EFIVARS DISK=$DISK VAR=$VAR"
out=$(run fwlk-guard-probe)
echo "$out"
fails=$((fails + $(grep -c '^FAIL' <<< "$out")))
[ "$(grep -c '^ok' <<< "$out")" = 19 ]; check $? "the probe ran all 19 checks"
[ ! -e "$VAR" ]; check $? "the test variable is gone from the firmware"

echo "== 2. the keeper inside the guard"
for way in /dev/mem "log variables"; do
	devmem=/dev/mem; [ "$way" = /dev/mem ] || devmem=/nonexistent
	unit fwlk-guard-keeper "$KEEPER" \
		"LogsDirectory=fwlk-guard-test/staging fwlk-guard-test/bootefi fwlk-guard-test/firmware" \
		"Environment=FWLK_MEMORY_ROOT=$T/staging FWLK_BOOTEFI_ROOT=$T/bootefi FWLK_MERGED_ROOT=$T/firmware FWLK_DEVMEM=$devmem"
	sed -i '/^LogsDirectory=firmware /d' /run/systemd/system/fwlk-guard-keeper.service  # the real archive stays read-only
	rc=0; out=$(run fwlk-guard-keeper) || rc=$?
	check $rc "through $way: the keeper ended in success"
	! grep -E '^(error|skipped memory)' <<< "$out"; check $? "through $way: no error, memory log read"
	got=$T/firmware/$(basename "$REAL")
	for f in firmware.log sources.tsv fpdt-boot.txt esrt.txt; do
		cmp -s "$got/$f" "$REAL/$f"; check $? "through $way: $f identical to the real archive's"
	done
	[ -z "$(ls -A "$T/staging")" ]; check $? "through $way: staging empty"
	rm -rf "$T"
done

echo "== guard score: $(systemd-analyze security "$PWD/$UNIT" --offline=yes --no-pager 2>/dev/null | tail -1)"
[ "$fails" = 0 ] && echo "all checks passed" || { echo "$fails check(s) failed"; exit 1; }
