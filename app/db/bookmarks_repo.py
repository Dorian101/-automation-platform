from collections.abc import Iterable

from app.core.identity import Identity, ids_of

from .database import Database

FIELDS = ("id", "url", "title", "description", "is_read", "source", "saved_at")
COLUMNS = ", ".join(FIELDS)


def _row(row) -> dict:
    return dict(zip(FIELDS, row))


class BookmarksRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def save(
        self,
        identity: Identity,
        readable: Identity | Iterable[Identity],
        url: str,
        description: str,
        source: str,
    ) -> tuple[dict, bool]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE bookmarks
                    SET saved_at = CURRENT_TIMESTAMP,
                        is_read = FALSE,
                        description = CASE WHEN %s <> '' THEN %s ELSE description END
                    WHERE id = (
                        SELECT id FROM bookmarks
                        WHERE url = %s AND user_id = ANY(%s)
                        ORDER BY id LIMIT 1
                    )
                    RETURNING {COLUMNS}
                    """,
                    (description, description, url, ids_of(readable)),
                )
                row = cur.fetchone()
                existed = row is not None

                if row is None:
                    cur.execute(
                        f"""
                        INSERT INTO bookmarks (user_id, url, description, source)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (user_id, url) DO UPDATE
                        SET saved_at = CURRENT_TIMESTAMP, is_read = FALSE
                        RETURNING {COLUMNS}
                        """,
                        (str(identity), url, description, source),
                    )
                    row = cur.fetchone()
            conn.commit()

        return _row(row), existed

    def entries(
        self, identity: Identity | Iterable[Identity], limit: int = 0
    ) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {COLUMNS} FROM bookmarks
                    WHERE user_id = ANY(%s)
                    ORDER BY saved_at DESC, id DESC
                    LIMIT %s
                    """,
                    (ids_of(identity), limit or None),
                )
                return [_row(row) for row in cur.fetchall()]

    def get(
        self, identity: Identity | Iterable[Identity], bookmark_id: int
    ) -> dict | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {COLUMNS} FROM bookmarks "
                    "WHERE id = %s AND user_id = ANY(%s)",
                    (bookmark_id, ids_of(identity)),
                )
                row = cur.fetchone()

        return None if row is None else _row(row)

    def fill_title(self, bookmark_id: int, title: str) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bookmarks SET title = %s WHERE id = %s AND title = ''",
                    (title, bookmark_id),
                )
            conn.commit()

    def update(
        self,
        identity: Identity | Iterable[Identity],
        bookmark_id: int,
        title: str,
        description: str,
    ) -> bool:
        return self._change(
            "title = %s, description = %s", (title, description), identity, bookmark_id
        )

    def mark(
        self, identity: Identity | Iterable[Identity], bookmark_id: int, is_read: bool
    ) -> bool:
        return self._change("is_read = %s", (is_read,), identity, bookmark_id)

    def delete(self, identity: Identity | Iterable[Identity], bookmark_id: int) -> bool:
        return self._one("DELETE FROM bookmarks", (), identity, bookmark_id)

    def _change(
        self, assignment: str, values: tuple, identity, bookmark_id: int
    ) -> bool:
        return self._one(
            f"UPDATE bookmarks SET {assignment}", values, identity, bookmark_id
        )

    def _one(self, statement: str, values: tuple, identity, bookmark_id: int) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"{statement} WHERE id = %s AND user_id = ANY(%s)",
                    (*values, bookmark_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0
