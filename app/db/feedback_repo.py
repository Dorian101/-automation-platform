"""Feedback left on the public pages, kept for the owner to read."""

from dataclasses import dataclass
from datetime import datetime

from .database import Database

#: How many entries the administration page shows. A guest book, not a log:
#: a page of them is everything there is for a long time.
DEFAULT_LIMIT = 100


@dataclass(frozen=True)
class Feedback:
    at: datetime
    page: str
    text: str


class FeedbackRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(self, text: str, page: str = "") -> None:
        """Keep one entry. Unlike the visit log a failure raises: the sender
        has to be told their text was not saved."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO feedback (page, text) VALUES (%s, %s)",
                    (page, text),
                )
            conn.commit()

    def recent(self, limit: int = DEFAULT_LIMIT) -> list[Feedback]:
        """The newest entries first."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT at, page, text FROM feedback "
                    "ORDER BY at DESC, id DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()

        return [Feedback(at=row[0], page=row[1], text=row[2]) for row in rows]
