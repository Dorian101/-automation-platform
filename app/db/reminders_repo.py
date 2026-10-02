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
