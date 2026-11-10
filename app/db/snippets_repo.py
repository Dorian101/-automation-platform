from collections.abc import Iterable

from app.core.identity import Identity, ids_of

from .database import Database

KEEP = 100


class SnippetsRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(self, identity: Identity, title: str, body: str, source: str) -> int:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO snippets (user_id, title, body, source) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (str(identity), title, body, source),
                )
                snippet_id = cur.fetchone()[0]
                cur.execute(
                    """
                    DELETE FROM snippets
                    WHERE user_id = %s AND id <= (
                        SELECT id FROM snippets WHERE user_id = %s
                        ORDER BY id DESC OFFSET %s LIMIT 1
                    )
                    """,
                    (str(identity), str(identity), KEEP),
                )
            conn.commit()

        return snippet_id

    def entries(
        self, identity: Identity | Iterable[Identity], limit: int = KEEP
    ) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, source, created_at,
                           octet_length(body),
                           array_length(string_to_array(body, E'\\n'), 1)
                    FROM snippets
                    WHERE user_id = ANY(%s)
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (ids_of(identity), limit),
                )
                return [
                    {
                        "id": row[0],
                        "title": row[1],
                        "source": row[2],
                        "created_at": row[3],
                        "bytes": row[4],
                        "lines": row[5] or 1,
                    }
                    for row in cur.fetchall()
                ]

    def get(
        self, identity: Identity | Iterable[Identity], snippet_id: int
    ) -> dict | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, body FROM snippets "
                    "WHERE id = %s AND user_id = ANY(%s)",
                    (snippet_id, ids_of(identity)),
                )
                row = cur.fetchone()

        if row is None:
            return None

        return {"id": row[0], "title": row[1], "body": row[2]}

    def update(
        self,
        identity: Identity | Iterable[Identity],
        snippet_id: int,
        title: str,
        body: str,
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE snippets SET title = %s, body = %s "
                    "WHERE id = %s AND user_id = ANY(%s)",
                    (title, body, snippet_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def delete(self, identity: Identity | Iterable[Identity], snippet_id: int) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM snippets WHERE id = %s AND user_id = ANY(%s)",
                    (snippet_id, ids_of(identity)),
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted > 0
