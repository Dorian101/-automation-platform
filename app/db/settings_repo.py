"""Switches the administrator sets from /admin/settings, kept in the database.

A missing key is the default — the `.env` value or the code's — so an empty
table changes nothing. Read on almost every request, so the whole table is
held in memory and re-read at most every few seconds; a write from this
process updates the copy at once.
"""

import logging
import time

from app.core.config import Config

from .database import Database

logger = logging.getLogger(__name__)

#: How stale the in-memory copy may get. Only matters for a value changed
#: outside this process, which today is nothing but a hand-run SQL statement.
CACHE_SECONDS = 10

MAINTENANCE = "maintenance"
DISABLED_PLUGINS = "plugins.disabled"
SIGNUP_OPEN = "signup.open"
FEEDBACK_PER_HOUR = "feedback.per_hour"
DEMO_PER_HOUR = "demo.per_hour"
COLLECTIONS_PER_USER = "collections.per_user"

#: Defaults for the limits, kept beside their keys so the settings page and
#: the readers agree on what "unset" means.
DEFAULT_FEEDBACK_PER_HOUR = 3
DEFAULT_COLLECTIONS_PER_USER = 3

#: Plugins that cannot be switched off: `system` answers /help and /start,
#: and without it a chat has no way to see what else is there.
ALWAYS_ON = frozenset({"system"})


class SettingsRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()
        self._values: dict[str, str] = {}
        self._read_at = float("-inf")

    def _all(self) -> dict[str, str]:
        if time.monotonic() - self._read_at > CACHE_SECONDS:
            # A database that cannot be read keeps the last known switches
            # rather than failing every command and page that asks for one.
            try:
                with self.database.connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT key, value FROM settings")
                        self._values = dict(cur.fetchall())
            except Exception:
                logger.warning("Settings unreadable; keeping the last values")
            self._read_at = time.monotonic()

        return self._values

    def get(self, key: str) -> str | None:
        return self._all().get(key)

    def put(self, key: str, value: str | None) -> None:
        """Store a value; None removes the key and so restores the default."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                if value is None:
                    cur.execute("DELETE FROM settings WHERE key = %s", (key,))
                else:
                    cur.execute(
                        "INSERT INTO settings (key, value) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        (key, value),
                    )
            conn.commit()

        self._read_at = float("-inf")

    def _flag(self, key: str, default: bool) -> bool:
        value = self.get(key)

        return default if value is None else value == "1"

    def _number(self, key: str, default: int) -> int:
        value = self.get(key)

        return int(value) if value is not None and value.isdigit() else default

    def maintenance(self) -> bool:
        return self._flag(MAINTENANCE, False)

    def disabled_plugins(self) -> set[str]:
        value = self.get(DISABLED_PLUGINS) or ""

        return {name for name in value.split(",") if name} - ALWAYS_ON

    def signup_open(self) -> bool:
        return self._flag(SIGNUP_OPEN, Config.SIGNUP_OPEN)

    def feedback_per_hour(self) -> int:
        return self._number(FEEDBACK_PER_HOUR, DEFAULT_FEEDBACK_PER_HOUR)

    def collections_per_user(self) -> int:
        return self._number(COLLECTIONS_PER_USER, DEFAULT_COLLECTIONS_PER_USER)

    def demo_per_hour(self) -> int:
        return self._number(DEMO_PER_HOUR, Config.DEMO_ATTEMPTS_PER_WINDOW)
