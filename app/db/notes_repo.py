from collections.abc import Iterable

from app.core.identity import Identity

from .database import Database


def _as_tuple(identity: Identity | Iterable[Identity]) -> tuple[Identity, ...]:
    """Accept one identity or several, so callers can widen a read."""
    if isinstance(identity, Identity):
        return (identity,)

    return tuple(identity)


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

    def list(self, identity: Identity | Iterable[Identity]) -> list[str]:
        """Return the notes, newest first.

        Accepts several identities so a linked person can be read as one
        history. Order is by id rather than by grouped identity, so notes from
        two transports interleave by when they were actually written instead of
        arriving as two blocks.
        """
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT text
                    FROM notes
                    WHERE user_id = ANY(%s)
                    ORDER BY id DESC
                    """,
                    ([str(one) for one in identities],),
                )

                return [row[0] for row in cur.fetchall()]
