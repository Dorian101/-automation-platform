from dataclasses import dataclass
from datetime import datetime

from .database import Database

MAX_PASSKEYS = 10


class TooManyPasskeys(Exception):
    pass


@dataclass(frozen=True)
class Passkey:
    id: int
    credential_id: bytes
    public_key: bytes
    sign_count: int
    label: str
    created_at: datetime
    used_at: datetime | None


COLUMNS = "id, credential_id, public_key, sign_count, label, created_at, used_at"


def _passkey(row) -> Passkey:
    return Passkey(row[0], bytes(row[1]), bytes(row[2]), *row[3:])


class PasskeysRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(
        self,
        user_id: int,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        label: str,
    ) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
                cur.execute(
                    "SELECT count(*) FROM passkeys WHERE user_id = %s", (user_id,)
                )

                if cur.fetchone()[0] >= MAX_PASSKEYS:
                    raise TooManyPasskeys

                cur.execute(
                    """
                    INSERT INTO passkeys
                        (user_id, credential_id, public_key, sign_count, label)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (user_id, credential_id, public_key, sign_count, label),
                )
            conn.commit()

    def for_user(self, user_id: int) -> list[Passkey]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {COLUMNS} FROM passkeys WHERE user_id = %s ORDER BY id",
                    (user_id,),
                )
                return [_passkey(row) for row in cur.fetchall()]

    def has_any(self, user_id: int) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM passkeys WHERE user_id = %s)",
                    (user_id,),
                )
                return cur.fetchone()[0]

    def find(self, user_id: int, credential_id: bytes) -> Passkey | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {COLUMNS} FROM passkeys "
                    "WHERE user_id = %s AND credential_id = %s",
                    (user_id, credential_id),
                )
                row = cur.fetchone()

        return _passkey(row) if row else None

    def used(self, passkey_id: int, sign_count: int) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE passkeys SET sign_count = %s, used_at = CURRENT_TIMESTAMP "
                    "WHERE id = %s",
                    (sign_count, passkey_id),
                )
            conn.commit()

    def remove(self, user_id: int, passkey_id: int) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM passkeys WHERE id = %s AND user_id = %s",
                    (passkey_id, user_id),
                )
                removed = cur.rowcount > 0
            conn.commit()

        return removed

    def remove_all(self, user_id: int) -> int:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM passkeys WHERE user_id = %s", (user_id,))
                removed = cur.rowcount
            conn.commit()

        return removed
