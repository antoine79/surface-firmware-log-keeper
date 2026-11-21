"""File log: one UEFI_Log<n>.txt of the EFI partition (SPEC 1.2). Read only, always."""
import re
from datetime import datetime

from .merge import norm

PADDING = b" \x00\r\n"
LINE = re.compile(rb"(\d\d):(\d\d):(\d\d)\.(\d{3}) :(?: (.*))?", re.S)
MARK = b"=== END_OF_LOG ==="
END = re.compile(rb"=== END_OF_LOG === @ === (.*?) ===")
CLOCK = re.compile(r"(\d+)/(\d+)/(\d+) (\d+):(\d+):(\d+)")


class FileLog:
    """lines: (line number, uptime in ms, message) of every log line before the first END_OF_LOG line."""

    def __init__(self, content):
        self.lines, self.complete, self.end_written, self.end_clock, self.trailing = [], False, "-", None, 0
        prev = pos = 0
        for n, raw in enumerate(content.split(b"\n"), 1):
            end = pos + len(raw)
            pos = end + 1
            if raw.endswith(b"\r"):
                raw, end = raw[:-1], end - 1
            if MARK in raw:
                self.complete, self.trailing = True, len(content) - end
                m = END.search(raw)
                if m:
                    self.end_written = m[1].decode("latin-1").replace("\t", " ").strip(" ") or "-"
                    c = CLOCK.fullmatch(self.end_written)
                    try:
                        self.end_clock = c and datetime(*map(int, c.groups()))
                    except (ValueError, OverflowError):  # not a date of the years 1 to 9999: no clock (1.2)
                        pass
                break
            if not raw:
                continue  # a blank line is not a log line, but it counts in the line numbers
            m = LINE.fullmatch(raw)
            if m:
                prev = ((int(m[1]) * 60 + int(m[2])) * 60 + int(m[3])) * 1000 + int(m[4])
            # a line that does not parse keeps the uptime of the line before it and its raw text
            self.lines.append((n, prev, norm((m[5] or b"" if m else raw).decode("latin-1"))))
