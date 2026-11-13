from collections.abc import Iterable

from psycopg.types.json import Jsonb

from app.core.identity import Identity, ids_of

from .database import Database

SHOWN_CHARS = 81


class CollectionsRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def owned(self, identity: Identity | Iterable[Identity]) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id, c.title, c.fields,
                           (SELECT count(*) FROM collection_records r
                            WHERE r.collection_id = c.id)
                    FROM collections c
                    WHERE c.user_id = ANY(%s)
                    ORDER BY c.id
                    """,
                    (ids_of(identity),),
                )
                return [
                    {"id": row[0], "title": row[1], "fields": row[2], "count": row[3]}
                    for row in cur.fetchall()
                ]

    def get(self, identity: Identity | Iterable[Identity], collection_id: int):
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, fields FROM collections "
                    "WHERE id = %s AND user_id = ANY(%s)",
                    (collection_id, ids_of(identity)),
                )
                row = cur.fetchone()

        return {"id": row[0], "title": row[1], "fields": row[2]} if row else None

    def create(
        self,
        identity: Identity,
        readable: Iterable[Identity],
        title: str,
        limit: int,
    ) -> int | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (min(ids_of(readable)),),
                )
                cur.execute(
                    "SELECT count(*) FROM collections WHERE user_id = ANY(%s)",
                    (ids_of(readable),),
                )
                if cur.fetchone()[0] >= limit:
                    return None
                cur.execute(
                    "INSERT INTO collections (user_id, title) VALUES (%s, %s) "
                    "RETURNING id",
                    (str(identity), title),
                )
                collection_id = cur.fetchone()[0]
            conn.commit()

        return collection_id

    def update(
        self,
        identity: Identity | Iterable[Identity],
        collection_id: int,
        title: str,
        fields: list[dict],
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE collections SET title = %s, fields = %s "
                    "WHERE id = %s AND user_id = ANY(%s)",
                    (title, Jsonb(fields), collection_id, ids_of(identity)),
                )
                changed = cur.rowcount > 0
            conn.commit()

        return changed

    def delete(
        self, identity: Identity | Iterable[Identity], collection_id: int
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM collections WHERE id = %s AND user_id = ANY(%s)",
                    (collection_id, ids_of(identity)),
                )
                removed = cur.rowcount > 0
            conn.commit()

        return removed

    def records(self, collection_id: int) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.id, (
                        SELECT coalesce(jsonb_object_agg(
                            item.key,
                            CASE WHEN jsonb_typeof(item.value) = 'string'
                                 THEN to_jsonb(left(item.value #>> '{}', %s))
                                 ELSE item.value END
                        ), '{}'::jsonb)
                        FROM jsonb_each(r.data) AS item
                    )
                    FROM collection_records r
                    WHERE r.collection_id = %s
                    ORDER BY r.id DESC
                    """,
                    (SHOWN_CHARS, collection_id),
                )
                return [{"id": row[0], "data": row[1]} for row in cur.fetchall()]

    def record(self, collection_id: int, record_id: int):
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM collection_records "
                    "WHERE id = %s AND collection_id = %s",
                    (record_id, collection_id),
                )
                row = cur.fetchone()

        return {"id": row[0], "data": row[1]} if row else None

    def add_record(
        self, identity: Identity, collection_id: int, data: dict, limit: int
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM collections WHERE id = %s FOR UPDATE",
                    (collection_id,),
                )
                cur.execute(
                    "SELECT count(*) FROM collection_records WHERE collection_id = %s",
                    (collection_id,),
                )
                if cur.fetchone()[0] >= limit:
                    return False
                cur.execute(
                    "INSERT INTO collection_records (collection_id, user_id, data) "
                    "VALUES (%s, %s, %s)",
                    (collection_id, str(identity), Jsonb(data)),
                )
            conn.commit()

        return True

    def update_record(self, collection_id: int, record_id: int, data: dict) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE collection_records SET data = %s, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = %s AND collection_id = %s",
                    (Jsonb(data), record_id, collection_id),
                )
                changed = cur.rowcount > 0
            conn.commit()

        return changed

    def delete_record(self, collection_id: int, record_id: int) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM collection_records "
                    "WHERE id = %s AND collection_id = %s",
                    (record_id, collection_id),
                )
                removed = cur.rowcount > 0
            conn.commit()

        return removed
