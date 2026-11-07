"""The application's ERROR records, kept where the admin area can show them.

The service user is deliberately not in `systemd-journal`, so the journal is
out of the application's reach. A logging handler writes each ERROR record to
`app_errors` instead, and the table keeps only the newest `KEEP` rows.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from .database import Database

#: How many records stay. A screen of recent failures, not an archive.
KEEP = 500

#: The longest traceback kept. Long enough for a full Python stack.
DETAILS_LIMIT = 8000

#: After a failed write, how long the handler leaves the database alone. A
#: database that is down would otherwise make every error wait for a pooled
#: connection, and the event loop waits with it.
PAUSE_SECONDS = 30

POLLING_LOGGER = "aiogram.dispatcher"
POLLING_FAILURE = "Failed to fetch updates"
OUTAGE_SECONDS = 300
OUTAGE_GAP_SECONDS = 60


@dataclass(frozen=True)
class AppError:
    at: datetime
    logger: str
    message: str
    details: str


class ErrorsRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(self, logger: str, message: str, details: str) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_errors (logger, message, details) "
                    "VALUES (%s, %s, %s)",
                    (logger, message, details[:DETAILS_LIMIT]),
                )
                cur.execute(
                    "DELETE FROM app_errors WHERE id <= "
                    "(SELECT max(id) - %s FROM app_errors)",
                    (KEEP,),
                )
            conn.commit()

    def recent(self, limit: int = 100) -> list[AppError]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT at, logger, message, details FROM app_errors "
                    "ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()

        return [AppError(*row) for row in rows]


class ErrorLogHandler(logging.Handler):
    """Writes ERROR and worse to `app_errors`.

    A failure to write is dropped silently: logging it would come straight
    back here, and a database that is down is already being reported by the
    record that could not be saved.
    """

    def __init__(self, repository: ErrorsRepository):
        super().__init__(level=logging.ERROR)
        self._repository = repository
        self._paused_until = 0.0
        self._outage_start = None
        self._outage_last = None
        self._outage_reported = False

    def emit(self, record: logging.LogRecord) -> None:
        if time.monotonic() < self._paused_until:
            return

        message = record.getMessage()

        if record.name == POLLING_LOGGER and message.startswith(POLLING_FAILURE):
            if not self._outage_lasts(record.created):
                return

            message = (
                f"Нет связи с Telegram дольше {OUTAGE_SECONDS // 60} минут: {message}"
            )

        try:
            details = ""

            if record.exc_info:
                details = logging.Formatter().formatException(record.exc_info)

            self._repository.add(record.name, message, details)
        except Exception:  # noqa: BLE001 — see the class docstring
            self._paused_until = time.monotonic() + PAUSE_SECONDS

    def _outage_lasts(self, now: float) -> bool:
        if self._outage_last is None or now - self._outage_last > OUTAGE_GAP_SECONDS:
            self._outage_start = now
            self._outage_reported = False

        self._outage_last = now

        if self._outage_reported or now - self._outage_start < OUTAGE_SECONDS:
            return False

        self._outage_reported = True
        return True
