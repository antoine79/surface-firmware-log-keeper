#!/usr/bin/python3
"""Keep the Surface Book 2 firmware's own logs, which nothing else on Fedora saves.

Installed as /usr/local/sbin/firmware-log-keeper by plans/2026-11-20-firmware-log-keeper.sh and run once per boot by
firmware-log-keeper.service. Standard library only.

  firmware-log-keeper                     save this boot's logs to /var/log/firmware/<UTC date, e.g. 2026-11-20T015000Z>_<boot id>/
  firmware-log-keeper decode-cper F...    decode saved HwErrRec*.bin files (4 attribute bytes + CPER record)
  firmware-log-keeper decode-advlog F     turn a saved advlog.bin (header + used entries) into text

Sources (all read-only; the firmware's variables are never written or deleted):
  HwErrRec* UEFI variables (CPER error records the firmware writes, it keeps only about 11 and rotates them),
  the Project Mu Advanced Logger buffer in reserved RAM (found through the AdvLoggerLocator variable, read via /dev/mem),
  ACPI FPDT boot timings, ESRT firmware versions.
Layouts: mu_plus AdvLoggerPkg/Include/AdvancedLoggerInternal.h, MsWheaPkg/Include/Guid/MuTelemetryCperSection.h,
UEFI spec appendix N (CPER).
"""
import glob
import hashlib
import lzma
import os
import shutil
import struct
import sys
import time
import uuid
from datetime import datetime, timedelta

ROOT = "/var/log/firmware"
ARCHIVE = f"{ROOT}/hwerrrec"
EFIVARS = "/sys/firmware/efi/efivars"
HWERR_GUID = "414e6bdd-e47b-47cc-b244-bb61020cf516"
ADVLOG_VAR = f"{EFIVARS}/AdvLoggerLocator-4d60cfb5-f481-4a98-9c81-bff86460c43e"
KEEP_BOOTS, KEEP_BYTES = 300, 500 * 1024 * 1024
COMPRESS_OVER = 1024 * 1024  # advlog files above this are written .xz

SEV = {0: "recoverable", 1: "fatal", 2: "corrected", 3: "informational"}
MU_TELEMETRY = uuid.UUID("85183a8b-9c41-429c-939c-5c3c087ca280")
COMPONENTS = {  # FILE_GUID of firmware drivers seen on this machine (grep FILE_GUID in ~/build/project-mu for others)
    uuid.UUID("5be3bdf4-53cf-46a3-a6a9-73c34a6e5ee3"): "NvmExpressDxe",
}
STATUS_CLASS = {0x00: "COMPUTING_UNIT", 0x01: "PERIPHERAL", 0x02: "IO_BUS", 0x03: "SOFTWARE"}
IO_BUS_SUB = {0x00: "UNSPECIFIED", 0x01: "PCI", 0x02: "USB", 0x03: "IBA", 0x04: "AGP", 0x05: "PC_CARD", 0x06: "LPC",
              0x07: "SCSI", 0x08: "ATA_ATAPI", 0x09: "FC", 0x0A: "IP_NETWORK", 0x0B: "SMBUS", 0x0C: "I2C"}
IOB_EC = {0: "NON_SPECIFIC", 1: "DISABLED", 2: "NOT_SUPPORTED", 3: "NOT_DETECTED", 4: "NOT_CONFIGURED",
          5: "INTERFACE_ERROR", 6: "CONTROLLER_ERROR", 7: "READ_ERROR", 8: "WRITE_ERROR", 9: "RESOURCE_CONFLICT"}


def status_name(v):
    cls, sub, op = v >> 24, (v >> 16) & 0xFF, v & 0xFFFF
    name = f"{STATUS_CLASS.get(cls, hex(cls))}"
    if cls == 0x02:
        name = f"EFI_IO_BUS_{IO_BUS_SUB.get(sub, hex(sub))}"
        if op < 0x1000:
            name += f"|EFI_IOB_EC_{IOB_EC.get(op, hex(op))}"
        else:
            name += f"|op 0x{op:04x}"
    else:
        name += f" sub 0x{sub:02x} op 0x{op:04x}"
    return name


def bcd(b):
    return (b >> 4) * 10 + (b & 0xF)


def decode_cper(raw, name=""):
    """One summary line per record, one indented line per section. raw includes the 4 efivar attribute bytes."""
    d = raw[4:]
    if d[:4] != b"CPER":
        return f"{name} not a CPER record: {d[:16].hex()}"
    nsec, sev, valid, rlen = struct.unpack_from("<HIII", d, 10)
    rec_id = struct.unpack_from("<Q", d, 96)[0]
    if valid & 2:
        s, mi, h, _flag, day, mo, y, c = d[24:32]
        # The RTC keeps local time and the firmware writes century 21 for 2026, so only the 2-digit year is trusted.
        ts = f"20{bcd(y):02d}-{bcd(mo):02d}-{bcd(day):02d} {bcd(h):02d}:{bcd(mi):02d}:{bcd(s):02d} local (raw century {c:02x})"
    else:
        ts = "no timestamp"
    out = [f"{name} record_id=0x{rec_id:x} {ts} severity={SEV.get(sev, sev)} length={rlen} sections={nsec}"]
    for i in range(nsec):
        o = 128 + 72 * i
        off, ln = struct.unpack_from("<II", d, o)
        st = uuid.UUID(bytes_le=d[o + 16:o + 32])
        ssev = struct.unpack_from("<I", d, o + 52)[0]
        body = d[off:off + ln]
        if st == MU_TELEMETRY and len(body) >= 56:
            comp = uuid.UUID(bytes_le=body[0:16])
            sub = uuid.UUID(bytes_le=body[16:32])
            status, a1, a2 = struct.unpack_from("<IQQ", body, 36)
            out.append(f"   Project Mu telemetry: component={comp} ({COMPONENTS.get(comp, 'unknown')}) "
                       f"subcomponent={sub} status=0x{status:08x} ({status_name(status)}) "
                       f"info1=0x{a1:x} info2=0x{a2:x} section_severity={SEV.get(ssev, ssev)}")
        else:
            out.append(f"   section type={st} severity={SEV.get(ssev, ssev)} body={body[:64].hex()}")
    return "\n".join(out)


def advlog_layout(buf, hdr_addr):
    """Return (start, end, info) as offsets into buf, which begins at the header (ADVANCED_LOGGER_INFO)."""
    sig, ver = struct.unpack_from("<4sH", buf, 0)
    if sig != b"ALOG":
        raise ValueError(f"no ALOG signature at 0x{hdr_addr:x}")
    p_start, p_cur, discarded, size = struct.unpack_from("<QQII", buf, 8)
    if hdr_addr <= p_start < hdr_addr + size + 4096:  # version 3 (this firmware): physical addresses at 8 and 16
        start, end = p_start - hdr_addr, p_cur - hdr_addr
    else:  # newer Mu: u32 LogBufferOffset at 12, LogCurrentOffset at 20
        start, end = struct.unpack_from("<I", buf, 12)[0], struct.unpack_from("<I", buf, 20)[0]
    if not (0 < start <= end <= start + size):
        raise ValueError(f"implausible log bounds start={start} end={end} size={size}")
    return start, end, dict(version=ver, discarded=discarded, size=size)


def advlog_text(buf, hdr_addr):
    start, end, info = advlog_layout(buf, hdr_addr)
    freq, ticks_at_time = struct.unpack_from("<QQ", buf, 0x28)
    year, mon, day, hour, minute, sec = struct.unpack_from("<H5B", buf, 0x38)
    out = [f"# Advanced Logger version {info['version']}, header at 0x{hdr_addr:x}, {end - start} bytes used of "
           f"{info['size']}, {info['discarded']} bytes discarded, timer {freq} Hz"]
    try:
        t0 = datetime(year, mon, day, hour, minute, sec) - timedelta(seconds=ticks_at_time / (freq or 1))
        out.append(f"# firmware clock (local time) read at tick {ticks_at_time}: {year}-{mon:02d}-{day:02d} "
                   f"{hour:02d}:{minute:02d}:{sec:02d}; columns: seconds since reset, firmware clock, debug level, text")
    except ValueError:
        t0 = None
    off, n, line, lt, ll = start, 0, "", None, None

    def emit(text, ts, lvl):
        secs = ts / freq if freq else 0
        clock = (t0 + timedelta(seconds=secs)).strftime("%H:%M:%S.%f")[:-3] if t0 else "-"
        out.append(f"{secs:9.3f} {clock} L{lvl:08x} {text.rstrip(chr(13))}")

    while off + 18 <= end:
        sig = buf[off:off + 4]
        if sig == b"ALMS":
            level, ts, mlen = struct.unpack_from("<IQH", buf, off + 4)
            text, size = buf[off + 18:off + 18 + mlen], (18 + mlen + 7) & ~7
        elif sig == b"ALM2":
            level, ts, _phase, mlen, moff = struct.unpack_from("<IQHHH", buf, off + 6)
            text, size = buf[off + moff:off + moff + mlen], (moff + mlen + 7) & ~7
        else:
            nxt = min([p for p in (buf.find(b"ALMS", off + 1, end), buf.find(b"ALM2", off + 1, end)) if p >= 0],
                      default=-1)
            if nxt < 0:
                break
            out.append(f"# resync: skipped {nxt - off} bytes at offset 0x{off:x}")
            off = nxt
            continue
        n += 1
        if lt is None:
            lt, ll = ts, level
        line += text.decode("latin-1")
        while "\n" in line:
            head, line = line.split("\n", 1)
            emit(head, lt, ll)
            lt, ll = (ts, level) if line else (None, None)
        off += size
    if line:
        emit(line, lt, ll)
    out.append(f"# {n} message entries decoded")
    return start, end, "\n".join(out) + "\n"


def write_maybe_xz(path, data):
    if len(data) > COMPRESS_OVER:
        with lzma.open(path + ".xz", "wb") as f:
            f.write(data)
    else:
        with open(path, "wb") as f:
            f.write(data)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def save_hwerr(outdir, notes):
    os.makedirs(ARCHIVE, exist_ok=True)
    new, lines = [], []
    for var in sorted(glob.glob(f"{EFIVARS}/HwErrRec*-*")):
        base = os.path.basename(var)
        raw = read(var)
        with open(f"{outdir}/{base}", "wb") as f:
            f.write(raw)
        if not base.endswith(HWERR_GUID) or base.startswith("HwErrRecSupport"):
            continue
        text = decode_cper(raw, base[:12])
        lines.append(text)
        h = hashlib.sha256(raw[4:]).hexdigest()[:16]
        dst = f"{ARCHIVE}/{h}.bin"
        if not os.path.exists(dst):
            open(dst + ".tmp", "wb").write(raw)
            os.rename(dst + ".tmp", dst)
            with open(f"{ARCHIVE}/index.txt", "a") as f:
                f.write(f"# first seen {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} as {base}, saved as {h}.bin\n{text}\n")
            new.append(text.splitlines()[0])
    with open(f"{outdir}/hwerrrec-decoded.txt", "w") as f:
        f.write("\n".join(lines) + "\n")
        f.write(f"# {len(new)} of these {len(lines)} records were new to {ARCHIVE}\n")
    if new:
        first = " ".join(new[0].split()[1:5])
        notes.append(f"{len(new)} new firmware hardware error record(s) (first: {first}); "
                     f"decoded in {outdir}/hwerrrec-decoded.txt and {ARCHIVE}/index.txt")


def save_advlog(outdir, notes):
    hdr_addr = struct.unpack_from("<Q", read(ADVLOG_VAR), 4)[0]
    fd = os.open("/dev/mem", os.O_RDONLY)
    try:
        head = os.pread(fd, 4096, hdr_addr)
        start, end, _ = advlog_layout(head, hdr_addr)
        buf = os.pread(fd, end, hdr_addr)  # header + used entries
    finally:
        os.close(fd)
    _, _, text = advlog_text(buf, hdr_addr)
    write_maybe_xz(f"{outdir}/advlog.bin", buf)
    write_maybe_xz(f"{outdir}/advlog.txt", text.encode())


def save_sysfs(outdir):
    with open(f"{outdir}/fpdt-boot.txt", "w") as f:
        for p in sorted(glob.glob("/sys/firmware/acpi/fpdt/boot/*")):
            f.write(f"{os.path.basename(p)}={read(p).decode().strip()}\n")
    with open(f"{outdir}/esrt.txt", "w") as f:
        for e in sorted(glob.glob("/sys/firmware/efi/esrt/entries/*")):
            vals = []
            for k in ("fw_class", "fw_version", "last_attempt_version", "last_attempt_status"):
                try:
                    vals.append(f"{k}={read(f'{e}/{k}').decode().strip()}")
                except OSError:
                    pass
            f.write(f"{os.path.basename(e)} {' '.join(vals)}\n")


def prune():  # never removes the newest folder (this boot)
    dirs = sorted(d for d in glob.glob(f"{ROOT}/2*_*") if os.path.isdir(d))  # names start with the date: sorted = oldest first
    sizes = {d: sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(d) for f in fs) for d in dirs}
    while len(dirs) > 1 and (len(dirs) > KEEP_BOOTS or sum(sizes[d] for d in dirs) > KEEP_BYTES):
        shutil.rmtree(dirs.pop(0))


def keep():
    boot_id = read("/proc/sys/kernel/random/boot_id").decode().strip().replace("-", "")
    existing = glob.glob(f"{ROOT}/*_{boot_id}")
    outdir = existing[0] if existing else f"{ROOT}/{time.strftime('%Y-%m-%dT%H%M%SZ', time.gmtime())}_{boot_id}"  # UTC: the laptop's time zone changes when travelling
    os.makedirs(outdir, exist_ok=True)
    notes, errors = [], []
    for step in (lambda: save_hwerr(outdir, notes), lambda: save_advlog(outdir, notes), lambda: save_sysfs(outdir)):
        try:
            step()
        except Exception as e:  # one broken source must not stop the others
            errors.append(f"{type(e).__name__}: {e}")
    prune()
    for n in notes:
        print(n)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    print(f"saved firmware logs to {outdir}")
    return 1 if errors else 0


def main(argv):
    if len(argv) > 1 and argv[1] == "decode-cper":
        for p in argv[2:]:
            print(decode_cper(read(p), os.path.basename(p)))
        return 0
    if len(argv) > 2 and argv[1] == "decode-advlog":
        p = argv[2]
        buf = lzma.open(p).read() if p.endswith(".xz") else read(p)
        # shortcut: assumes the header is page aligned (true on this machine); pass the address if that ever changes
        hdr = int(argv[3], 0) if len(argv) > 3 else struct.unpack_from("<Q", buf, 8)[0] & ~0xFFF
        sys.stdout.write(advlog_text(buf, hdr)[2])
        return 0
    if len(argv) > 1:
        print(__doc__)
        return 2
    return keep()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
