from datetime import datetime

from psycopg.types.json import Jsonb

from .database import Database


class HeldNotificationsRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def hold(self, user_id: int, target: str, body: dict) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO held_notifications (user_id, target, body) "
                    "VALUES (%s, %s, %s)",
                    (user_id, target, Jsonb(body)),
                )
            conn.commit()

    def entries(self) -> list[tuple[int, int, str, dict, datetime]]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, target, body, created_at "
                    "FROM held_notifications ORDER BY id"
                )
                return cur.fetchall()

    def release(self, held_id: int) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM held_notifications WHERE id = %s", (held_id,))
            conn.commit()
