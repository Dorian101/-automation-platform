from app.core.identity import Identity

from .database import Database


class NotesRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(self, identity: Identity, text: str) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO notes (user_id, text)
                    VALUES (%s, %s)
                    """,
                    (str(identity), text),
                )
            conn.commit()

    def list(self, identity: Identity) -> list[str]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT text
                    FROM notes
                    WHERE user_id = %s
                    ORDER BY id DESC
                    """,
                    (str(identity),),
                )

                return [row[0] for row in cur.fetchall()]
