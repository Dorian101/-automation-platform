from .database import Database
from datetime import datetime
from typing import List, Tuple


class RemindersRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()
        self._init_table()

    def _init_table(self):
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                text TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                is_sent INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

    def add(self,chat_id:int, text: str, remind_at: str) -> None:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO reminders (chat_id, text, remind_at) VALUES (?, ?, ?)",
            (chat_id, text, remind_at),
        )
        conn.commit()
        conn.close()

    def get_due(self) -> List[Tuple[int, str]]:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id,chat_id, text FROM reminders
            WHERE is_sent = 0 AND remind_at <= ?
        """, (datetime.utcnow().isoformat(),))
        
        result = cursor.fetchall()
        conn.close()
        
        return result

    def mark_sent(self, reminder_id: int) -> None:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE reminders SET is_sent = 1 WHERE id = ?",
            (reminder_id,),
        )
        conn.commit()
        conn.close()