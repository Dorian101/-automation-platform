from app.core.identity import Identity

from .database import Database


class ClipboardRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def save(self, identity: Identity, text: str, created_at: str) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clipboard (user_id, text, created_at) "
                    "VALUES (%s, %s, %s)",
                    (str(identity), text, created_at),
                )
            conn.commit()

    def get_last(self, identity: Identity) -> str | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT text
                    FROM clipboard
                    WHERE user_id = %s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (str(identity),),
                )

                result = cur.fetchone()

                return result[0] if result else None
