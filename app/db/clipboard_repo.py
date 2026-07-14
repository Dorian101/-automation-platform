from typing import Optional

from .database import Database


class ClipboardRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()
        self._init_table()

    def _init_table(self):
        conn = self.database.connect()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS clipboard (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()
        conn.close()

    def save(self, text: str, created_at: str, chat_id: int):
        conn = self.database.connect()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO clipboard (text, created_at, chat_id) VALUES (?, ?, ?)",
            (text, created_at, chat_id),
        )

        conn.commit()
        conn.close()

    def get_last(self, chat_id: int) -> Optional[str]:
        conn = self.database.connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT text
            FROM clipboard
            where chat_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (chat_id,))

        result = cursor.fetchone()
        conn.close()

        if result:
            return result[0]

        return None