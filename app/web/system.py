"""System diagnostics for the status page, from the standard library only.

What the status page shows about the host — disk, memory, load — is read from
`/proc` and `os.statvfs` rather than from `psutil`. The platform already runs
on a Linux host under a systemd sandbox that keeps `/proc` readable
(`ProtectProc=default`, `ProcSubset=all`), and `statvfs` is a read-only call
that the `ProtectSystem=strict` mount does not block. A dependency for three
numbers is not needed.

Every function returns a plain dict or a scalar, never a dataclass, so the one
caller can serialize it straight to JSON.
"""

import os
from collections.abc import Iterable

# Matches the "MemAvailable" line in /proc/meminfo, written in kibibytes.
# No colon: `line.partition(":")` already strips it from the key, and these
# are compared against that colon-less key.
_MEM_TOTAL = "MemTotal"
_MEM_AVAILABLE = "MemAvailable"


def disk() -> dict:
    """Free and used space on the root filesystem, in bytes.

    The one volume the service lives on. `os.statvfs` is what `df` reads. The
    backups' own size is reported by the backups card, not by this figure.
    """
    return {"root": _volume("/")}


def _volume(path: str) -> dict:
    try:
        stat = os.statvfs(path)
    except OSError:
        return {"total": 0, "used": 0, "free": 0}

    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    used = total - stat.f_bfree * stat.f_frsize

    return {"total": total, "used": used, "free": free}


def memory() -> dict:
    """RAM totals, in bytes, straight from /proc/meminfo.

    None rather than zero when the file cannot be read: a dashboard printing
    0 B on a host that keeps its memory elsewhere is a dashboard lying about
    an idle box.
    """
    values = _meminfo()

    if values is None:
        return {"total": None, "available": None}

    return {
        "total": values[_MEM_TOTAL],
        "available": values[_MEM_AVAILABLE],
    }


def load() -> float | None:
    """The one-minute load average, or None when /proc is not readable.

    None rather than a fabricated number: a dashboard that could not read the
    host should say so, not print a zero that reads as "idle".
    """
    try:
        with open("/proc/loadavg", encoding="ascii") as source:
            return float(source.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def cores() -> int | None:
    """How many logical CPUs the host has, or None when it cannot be known.

    Load average is only meaningful against this number: a load of 2 is idle
    on four cores and maxed on two. A dashboard that shows the raw load
    without the core count invites a false alarm.
    """
    return os.cpu_count()


def _meminfo() -> dict[str, int] | None:
    """Total and available memory, in bytes, from /proc/meminfo.

    None when the file is unreadable, so a caller can tell "nothing to show"
    from "no memory". Every value is otherwise bytes.
    """
    try:
        with open("/proc/meminfo", encoding="ascii") as source:
            return _parse_meminfo(source)
    except (OSError, ValueError, IndexError):
        return None


def _parse_meminfo(lines: Iterable[str]) -> dict[str, int]:
    """Bytes for the two figures the dashboard needs, from meminfo lines.

    Pure so the parsing is testable without touching the filesystem: the
    numbers come from `/proc` on the host, which a test suite cannot read.
    A line is `key` up to the first colon and a value in kibibytes after it;
    the bytes follow by multiplying by 1024.
    """
    found: dict[str, int] = {
        _MEM_TOTAL: 0,
        _MEM_AVAILABLE: 0,
    }

    for line in lines:
        key, _, value = line.partition(":")
        if key in found:
            # "MemAvailable:   1331012 kB" -> bytes
            kbytes = value.strip().split()[0]
            found[key] = int(kbytes) * 1024

    return found
