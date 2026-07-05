import sqlite3
from typing import List


class NotesRepository:
    def __init__(self, db_path: str = "app.db"):
        self.conn = sqlite3.connect(db_path)
        self._init_table()

    def _init_table(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def add(self, text: str) -> None:
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO notes (text) VALUES (?)", (text,))
        self.conn.commit()

    def list(self) -> List[str]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT text FROM notes ORDER BY id DESC")
        return [row[0] for row in cursor.fetchall()]