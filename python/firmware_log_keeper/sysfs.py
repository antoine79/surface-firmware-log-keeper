"""Boot timings and firmware versions, copied from sysfs as text (SPEC 1 and 2). None when there is nothing."""
import os


def _value(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace").strip(" \t\r\n")


def fpdt(root):
    """One '<file name>=<value>' line per file, sorted by name."""
    try:
        names = sorted(n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n)))
    except OSError:
        return None
    return "".join(f"{n}={_value(os.path.join(root, n))}\n" for n in names).encode() or None


def esrt(root):
    """One line per entry, sorted: '<entry> fw_class=... fw_version=... last_attempt_version=... last_attempt_status=...'."""
    try:
        entries = sorted(e for e in os.listdir(root) if os.path.isdir(os.path.join(root, e)))
    except OSError:
        return None
    out = ""
    for e in entries:
        values = []
        for k in ("fw_class", "fw_version", "last_attempt_version", "last_attempt_status"):
            try:
                values.append(f"{k}={_value(os.path.join(root, e, k))}")
            except OSError:
                pass
        out += f"{e} {' '.join(values)}\n"
    return out.encode() or None
