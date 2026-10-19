from datetime import datetime

from app.core.identity import Identity

from .database import Database


class RemindersRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(self, identity: Identity, text: str, remind_at: str) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO reminders (user_id, text, remind_at)
                    VALUES (%s, %s, %s::timestamp)
                    """,
                    (str(identity), text, remind_at),
                )
            conn.commit()

    def get_due(self) -> list[tuple[int, str, str]]:
        """Return ``(id, user_id, text)`` for every reminder that is due."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, text
                    FROM reminders
                    WHERE is_sent = FALSE
                        AND remind_at <= (NOW() AT TIME ZONE 'UTC')
                    """,
                )

                return cur.fetchall()

    def entries(
        self,
        identity: Identity,
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
                    WHERE user_id = %s AND is_sent = FALSE
                    ORDER BY remind_at, id
                    """,
                    (str(identity),),
                )

                return cur.fetchall()

    def delete(self, identity: Identity, reminder_id: int) -> bool:
        """Remove one unsent reminder, reporting whether it was there.

        Scoped to the account as well as to the id. The id is a global
        sequence, so scoping by id alone would let one person's reminder be
        cancelled by another guessing a number.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM reminders
                    WHERE id = %s AND user_id = %s AND is_sent = FALSE
                    """,
                    (reminder_id, str(identity)),
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted > 0

    def mark_sent(self, reminder_id: int) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE reminders
                    SET is_sent = TRUE
                    WHERE id = %s
                    """,
                    (reminder_id,),
                )
            conn.commit()
