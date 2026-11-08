import re
import secrets

from app.core.identity import WEB, Identity

from .database import Database
from .sessions_repo import digest

PREFIX = "ap_"
MAX_KEYS = 5
KEY_SHAPE = re.compile(re.escape(PREFIX) + r"[A-Za-z0-9_-]{43}")


class TooManyKeys(Exception):
    pass


class ApiKeysRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def create(self, identity: Identity, name: str) -> str | None:
        if identity.kind != WEB:
            return None

        key = PREFIX + secrets.token_urlsafe(32)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM users "
                    "WHERE lower(username) = lower(%s) AND is_active FOR UPDATE",
                    (identity.id,),
                )
                row = cur.fetchone()

                if row is None:
                    return None

                cur.execute(
                    "SELECT count(*) FROM api_keys WHERE user_id = %s", (row[0],)
                )

                if cur.fetchone()[0] >= MAX_KEYS:
                    raise TooManyKeys

                cur.execute(
                    "INSERT INTO api_keys (user_id, name, key_hash) "
                    "VALUES (%s, %s, %s)",
                    (row[0], name, digest(key)),
                )
            conn.commit()

        return key

    def entries(self, identity: Identity) -> list[dict]:
        if identity.kind != WEB:
            return []

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT k.id, k.name, k.created_at, k.last_used_at
                    FROM api_keys k JOIN users u ON u.id = k.user_id
                    WHERE lower(u.username) = lower(%s)
                    ORDER BY k.id
                    """,
                    (identity.id,),
                )
                return [
                    {"id": r[0], "name": r[1], "created_at": r[2], "last_used_at": r[3]}
                    for r in cur.fetchall()
                ]

    def revoke(self, identity: Identity, key_id: int) -> bool:
        if identity.kind != WEB:
            return False

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM api_keys
                    WHERE id = %s AND user_id = (
                        SELECT id FROM users WHERE lower(username) = lower(%s)
                    )
                    """,
                    (key_id, identity.id),
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted > 0

    def identity_for(self, key: str) -> Identity | None:
        if not isinstance(key, str) or not KEY_SHAPE.fullmatch(key):
            return None

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT k.id, u.username,
                           k.last_used_at IS NULL
                           OR k.last_used_at < CURRENT_TIMESTAMP - interval '1 hour'
                    FROM api_keys k JOIN users u ON u.id = k.user_id
                    WHERE k.key_hash = %s AND u.is_active
                    """,
                    (digest(key),),
                )
                row = cur.fetchone()

                if row is not None and row[2]:
                    cur.execute(
                        "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP "
                        "WHERE id = %s",
                        (row[0],),
                    )
            conn.commit()

        return None if row is None else Identity(kind=WEB, id=row[1])
