import sqlite3
from datetime import datetime
from typing import List, Tuple


class RemindersRepository:
    def __init__(self, db_path: str = "app.db"):
        self.conn = sqlite3.connect(db_path)
        self._init_table()

    def _init_table(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                text TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                is_sent INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()

    def add(self,chat_id:int, text: str, remind_at: str) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO reminders (chat_id, text, remind_at) VALUES (?, ?, ?)",
            (chat_id, text, remind_at),
        )
        self.conn.commit()

    def get_due(self) -> List[Tuple[int, str]]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id,chat_id, text FROM reminders
            WHERE is_sent = 0 AND remind_at <= ?
        """, (datetime.utcnow().isoformat(),))
        return cursor.fetchall()

    def mark_sent(self, reminder_id: int) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE reminders SET is_sent = 1 WHERE id = ?",
            (reminder_id,),
        )
        self.conn.commit()