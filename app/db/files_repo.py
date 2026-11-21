from collections.abc import Iterable

from app.core.identity import Identity, ids_of

from .database import Database

FIELDS = ("id", "name", "size", "kind", "stored", "left")


def _live(p: str = "") -> str:
    return f"{p}expires_at > CURRENT_TIMESTAMP"


def _left(p: str = "") -> str:
    return f"EXTRACT(EPOCH FROM {p}expires_at - CURRENT_TIMESTAMP)::int"


def _columns(p: str = "") -> str:
    return ", ".join(f"{p}{field}" for field in FIELDS[:-1]) + ", " + _left(p)


COLUMNS = _columns()
LIVE = _live()


def _row(row) -> dict:
    return dict(zip(FIELDS, row))


class FilesRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(
        self, identity: Identity, name: str, size: int, kind: str, stored: str
    ) -> dict:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO files (user_id, name, size, kind, stored)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING {COLUMNS}
                    """,
                    (str(identity), name, size, kind, stored),
                )
                row = cur.fetchone()
            conn.commit()

        return _row(row)

    def entries(self, readable: Identity | Iterable[Identity]) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {COLUMNS} FROM files "
                    f"WHERE user_id = ANY(%s) AND {LIVE} ORDER BY id DESC",
                    (ids_of(readable),),
                )
                rows = cur.fetchall()

        return [_row(row) for row in rows]

    def get(self, readable: Identity | Iterable[Identity], file_id: int) -> dict | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {COLUMNS} FROM files "
                    f"WHERE id = %s AND user_id = ANY(%s) AND {LIVE}",
                    (file_id, ids_of(readable)),
                )
                row = cur.fetchone()

        return None if row is None else _row(row)

    def delete(
        self, readable: Identity | Iterable[Identity], file_id: int
    ) -> str | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM files WHERE id = %s AND user_id = ANY(%s) "
                    "RETURNING stored",
                    (file_id, ids_of(readable)),
                )
                row = cur.fetchone()
            conn.commit()

        return None if row is None else row[0]

    def used_bytes(self, owners: str = "%") -> int:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(size), 0) FROM files WHERE user_id LIKE %s",
                    (owners,),
                )
                return int(cur.fetchone()[0])

    def stored_names(self) -> set[str]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT stored FROM files")
                return {row[0] for row in cur.fetchall()}

    def purge(self) -> list[str]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM files WHERE expires_at <= CURRENT_TIMESTAMP "
                    "RETURNING stored"
                )
                gone = [row[0] for row in cur.fetchall()]
                cur.execute(
                    "DELETE FROM shares WHERE expires_at <= CURRENT_TIMESTAMP "
                    "OR NOT EXISTS (SELECT 1 FROM share_files "
                    "WHERE share_id = shares.id)"
                )
            conn.commit()

        return gone

    def share(
        self,
        identity: Identity,
        readable: Identity | Iterable[Identity],
        file_ids: list[int],
        token_hash: str,
    ) -> int:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, expires_at FROM files "
                    f"WHERE id = ANY(%s) AND user_id = ANY(%s) AND {LIVE}",
                    (file_ids, ids_of(readable)),
                )
                found = cur.fetchall()

                if len(found) != len(set(file_ids)):
                    return 0

                cur.execute(
                    "INSERT INTO shares (user_id, token_hash, expires_at) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (str(identity), token_hash, max(row[1] for row in found)),
                )
                share_id = cur.fetchone()[0]
                cur.executemany(
                    "INSERT INTO share_files (share_id, file_id, user_id) "
                    "VALUES (%s, %s, %s)",
                    [(share_id, row[0], str(identity)) for row in found],
                )
            conn.commit()

        return len(found)

    def shares(self, readable: Identity | Iterable[Identity]) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT s.id,
                        {_left("s.")},
                        COUNT(f.id)
                    FROM shares s
                    JOIN share_files sf ON sf.share_id = s.id
                    JOIN files f ON f.id = sf.file_id AND {_live("f.")}
                    WHERE s.user_id = ANY(%s) AND {_live("s.")}
                    GROUP BY s.id ORDER BY s.id DESC
                    """,
                    (ids_of(readable),),
                )
                rows = cur.fetchall()

        return [{"id": row[0], "left": row[1], "files": row[2]} for row in rows]

    def revoke(self, readable: Identity | Iterable[Identity], share_id: int) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM shares WHERE id = %s AND user_id = ANY(%s)",
                    (share_id, ids_of(readable)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def shared(self, token_hash: str, file_id: int | None = None) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_columns("f.")}
                    FROM shares s
                    JOIN share_files sf ON sf.share_id = s.id
                    JOIN files f ON f.id = sf.file_id AND {_live("f.")}
                    WHERE s.token_hash = %s AND {_live("s.")}
                    AND (%s::int IS NULL OR f.id = %s)
                    ORDER BY f.id
                    """,
                    (token_hash, file_id, file_id),
                )
                rows = cur.fetchall()

        return [_row(row) for row in rows]
