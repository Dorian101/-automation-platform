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

# Matches the "MemAvailable" line in /proc/meminfo, written in kibibytes.
_MEM_TOTAL = "MemTotal:"
_MEM_AVAILABLE = "MemAvailable:"


def disk() -> dict:
    """Free and used space on the two volumes the service touches.

    The application lives on the root filesystem; the backups live on a
    dedicated path. Both answers come from `os.statvfs`, which is what `df`
    reads, and the numbers are bytes.
    """
    return {
        "root": _volume("/"),
        "backups": _volume(_backups_volume()),
    }


def _backups_volume() -> str:
    # The backups directory is a config away from the working tree; the exact
    # path is not this module's business, so it reads the same setting the
    # backup manager does rather than hardcoding a second path.
    from app.db.backup import default_backups_dir

    return str(default_backups_dir())


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
    found: dict[str, int] = {
        _MEM_TOTAL: 0,
        _MEM_AVAILABLE: 0,
    }

    try:
        with open("/proc/meminfo", encoding="ascii") as source:
            for line in source:
                key, _, value = line.partition(":")
                if key in found:
                    # "MemAvailable:   1331012 kB" -> bytes
                    kbytes = value.strip().split()[0]
                    found[key] = int(kbytes) * 1024
    except (OSError, ValueError, IndexError):
        return None

    return found
