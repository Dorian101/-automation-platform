from collections.abc import Iterable

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

    def get_last(self, identity: Identity | Iterable[Identity]) -> str | None:
        """Return the most recently copied text, whichever transport copied it.

        The clipboard is one buffer shared between two entry points, so the last
        write wins no matter where it came from. Copying on the web and pasting
        in the bot is the case that makes the pairing worth having.
        """
        identities = (
            (identity,)
            if isinstance(identity, Identity)
            else tuple(identity)
        )

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT text
                    FROM clipboard
                    WHERE user_id = ANY(%s)
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    ([str(one) for one in identities],),
                )

                result = cur.fetchone()

        return result[0] if result else None
