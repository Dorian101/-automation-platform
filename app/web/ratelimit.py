"""Sliding-window rate limiting for the public forms.

The web interface runs as a single process against one loopback port, so an
in-process store is all the sharing there is to be done: there is no second
process for two windows to disagree about. Clock comes from `monotonic`, which
cannot be shifted by an attacker the way wall time can.

The client address is read from `X-Forwarded-For`, which is only trustworthy
because every request into the application has already passed the transport
check: the header is appended by the reverse proxy, the only way in.

Denied attempts are not recorded, and neither are their keys. That is what
keeps the store bounded: someone hammering the door fills the window up to
the threshold and no further, and the number of keys they can create
follows the attempts they were *allowed* rather than the attempts they made.

Two things about the key space are worth saying out loud, because the
obvious reading assumes them away. A key is caller-supplied and login folds
a username into it, so the store is only as bounded as whatever bounds those
keys - see `LOGIN_ATTEMPTS_PER_ADDRESS_PER_WINDOW`, which is what keeps login
to a fixed number of keys per address per window instead of one per name ever
tried. And cleanup is lazy and per key, so a window belonging to an address
that never comes back stays in the dict forever. Hence the ceiling below, as
a backstop for a caller that finds a way to produce keys the limits do not
bound.
"""

import time
from collections import deque

from aiohttp import web


def client_ip(request: web.Request) -> str:
    """The caller's address, for logging and for rate limiting.

    Read from the proxy's header, not the socket: every request has already
    passed the X-Platform-Auth transport check by the time it gets here, so
    the header can only have been written by the reverse proxy.

    Lives here rather than in `server.py` because the demonstration needs it
    too, and importing the other way round would be a cycle.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")

    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.remote or "unknown"


#: A ceiling on stored windows, generous enough that nothing real comes near
#: it and small enough that a mistake in a limit costs memory rather than the
#: host. Dicts keep insertion order, so the oldest window is the first key.
MAX_KEYS = 4096


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        """Record one attempt, if it is within the allowance.

        Returns False once the window holds `limit` attempts or more; a denied
        attempt leaves the window exactly as it was, including the store: a
        denied attempt must not create the key it was refused on, or the number
        of keys follows the attempts made rather than the attempts allowed.
        """
        if limit <= 0:
            return True

        now = time.monotonic()
        hits = self._hits.get(key)

        if hits is None:
            hits = deque()
            self._hits[key] = hits
            self._evict_oldest()

        while hits and now - hits[0] > window_seconds:
            hits.popleft()

        if len(hits) >= limit:
            return False

        hits.append(now)

        return True

    def _evict_oldest(self) -> None:
        """Drop the longest-sitting windows until the store is under its ceiling.

        Nothing here checks whether a window has aged out. The ceiling exists to
        bound memory; deciding which keys are worth keeping is the sort of
        cleverness that makes a limiter wrong in a new way.
        """
        while len(self._hits) > MAX_KEYS:
            del self._hits[next(iter(self._hits))]

    def clear(self, key: str) -> None:
        """Forget an address's history — used on a successful login.

        Without this, a correct password just typed after a handful of typos
        would land on the same block as the typos themselves.
        """
        self._hits.pop(key, None)
