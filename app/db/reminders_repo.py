from collections.abc import Iterable
from datetime import datetime

from app.core.identity import Identity

from .database import Database


def _ids(identity: Identity | Iterable[Identity]) -> list[str]:
    if isinstance(identity, Identity):
        return [str(identity)]

    return [str(one) for one in identity]


class RemindersRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(
        self,
        identity: Identity,
        text: str,
        remind_at: str | datetime,
        repeat: str | None = None,
    ) -> int:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO reminders (user_id, text, remind_at, repeat)
                    VALUES (%s, %s, %s::timestamp, %s)
                    RETURNING id
                    """,
                    (str(identity), text, remind_at, repeat),
                )
                reminder_id = cur.fetchone()[0]
            conn.commit()

        return reminder_id

    def get_due(self) -> list[tuple[int, str, str, datetime, str | None]]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, text, remind_at, repeat
                    FROM reminders
                    WHERE is_sent = FALSE
                        AND remind_at <= (NOW() AT TIME ZONE 'UTC')
                    """,
                )

                return cur.fetchall()

    def entries(
        self,
        identity: Identity | Iterable[Identity],
    ) -> list[tuple[int, str, datetime]]:
        """Return ``(id, text, remind_at)`` for every unsent reminder.

        Soonest first, so the numbered list a person reads puts the nearest
        reminder at the top. The id comes back with the text because it is how
        one is addressed for `/cancel`: a position in this list would move as
        soon as anything was added.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, text, remind_at
                    FROM reminders
                    WHERE user_id = ANY(%s) AND is_sent = FALSE
                    ORDER BY remind_at, id
                    """,
                    (_ids(identity),),
                )

                return cur.fetchall()

    def page_entries(
        self, identity: Identity | Iterable[Identity], fired_since: datetime
    ) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, text, remind_at, repeat, is_sent
                    FROM reminders
                    WHERE user_id = ANY(%s)
                        AND (is_sent = FALSE OR remind_at >= %s)
                    ORDER BY is_sent DESC,
                        CASE WHEN is_sent THEN remind_at END DESC,
                        remind_at, id
                    """,
                    (_ids(identity), fired_since),
                )
                rows = cur.fetchall()

        return [
            {"id": row[0], "text": row[1], "remind_at": row[2], "repeat": row[3],
             "sent": row[4]}
            for row in rows
        ]

    def delete(
        self, identity: Identity | Iterable[Identity], reminder_id: int
    ) -> bool:
        """Remove one reminder, reporting whether it was there.

        Scoped to the account as well as to the id. The id is a global
        sequence, so scoping by id alone would let one person's reminder be
        cancelled by another guessing a number.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM reminders
                    WHERE id = %s AND user_id = ANY(%s)
                    """,
                    (reminder_id, _ids(identity)),
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted > 0

    def snooze(
        self,
        identity: Identity | Iterable[Identity],
        reminder_id: int,
        remind_at: datetime,
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE reminders
                    SET remind_at = %s,
                        repeat = CASE WHEN is_sent THEN NULL ELSE repeat END,
                        is_sent = FALSE
                    WHERE id = %s AND user_id = ANY(%s)
                    """,
                    (remind_at, reminder_id, _ids(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def mark_sent(self, reminder_id: int, next_at: datetime | None = None) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE reminders
                    SET is_sent = TRUE
                    WHERE id = %s AND is_sent = FALSE
                    RETURNING user_id, text, repeat
                    """,
                    (reminder_id,),
                )
                row = cur.fetchone()
                if row is not None and next_at is not None:
                    cur.execute(
                        """
                        INSERT INTO reminders (user_id, text, remind_at, repeat)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (row[0], row[1], next_at, row[2]),
                    )
            conn.commit()
