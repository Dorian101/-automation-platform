from collections.abc import Iterable

from app.core.identity import Identity, as_tuple, ids_of

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

    def entries(
        self,
        identity: Identity | Iterable[Identity],
    ) -> list[tuple[int, str]]:
        """Return ``(id, text)`` for every note, newest first.

        The id comes back with the text because it is how a note is addressed
        when deleting one: a position in this list would move as soon as
        anything was added, and the text is not unique.

        Accepts several identities so a linked person can be read as one
        history. Order is by id rather than by grouped identity, so notes from
        two transports interleave by when they were actually written instead of
        arriving as two blocks.
        """
        identities = as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, text
                    FROM notes
                    WHERE user_id = ANY(%s)
                    ORDER BY id DESC
                    """,
                    (ids_of(identities),),
                )

                return [(row[0], row[1]) for row in cur.fetchall()]

    def count(self, identity: Identity | Iterable[Identity]) -> int:
        """How many notes this read would see."""
        identities = as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM notes WHERE user_id = ANY(%s)",
                    (ids_of(identities),),
                )

                return cur.fetchone()[0]

    def delete(
        self,
        identity: Identity | Iterable[Identity],
        note_id: int,
    ) -> bool:
        """Remove one note, reporting whether it was there.

        Scoped to the identities being read rather than to the id alone. The id
        is a global sequence, so scoping by id would let one person's note be
        deleted by another guessing a number — and the same widening that lets
        a linked pair read across transports has to let it delete across them,
        or a note visible in the console could not be removed from it.
        """
        identities = as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM notes
                    WHERE id = %s AND user_id = ANY(%s)
                    """,
                    (note_id, ids_of(identities)),
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted > 0

    def clear(self, identity: Identity | Iterable[Identity]) -> int:
        """Remove every note this read would see, returning how many."""
        identities = as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM notes WHERE user_id = ANY(%s)",
                    (ids_of(identities),),
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted
