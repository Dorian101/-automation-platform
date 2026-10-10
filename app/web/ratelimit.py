"""Sliding-window rate limiting for the public forms.

The web interface runs as a single process against one loopback port, so an
in-process store is all the sharing there is to be done: there is no second
process for two windows to disagree about. Clock comes from `monotonic`, which
cannot be shifted by an attacker the way wall time can.

The client address is read from `X-Forwarded-For`, which is only trustworthy
because every request into the application has already passed the transport
check: the header is appended by the reverse proxy, the only way in.

Denied attempts are not recorded, which keeps the per-key memory bounded by the
limit itself: someone hammering the door fills the window up to the threshold
and no further.
"""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        """Record one attempt, if it is within the allowance.

        Returns False once the window holds `limit` attempts or more; a denied
        attempt leaves the window exactly as it was.
        """
        if limit <= 0:
            return True

        now = time.monotonic()
        hits = self._hits[key]

        while hits and now - hits[0] > window_seconds:
            hits.popleft()

        if len(hits) >= limit:
            return False

        hits.append(now)

        return True

    def clear(self, key: str) -> None:
        """Forget an address's history — used on a successful login.

        Without this, a correct password just typed after a handful of typos
        would land on the same block as the typos themselves.
        """
        self._hits.pop(key, None)
