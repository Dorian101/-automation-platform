import sqlite3
from typing import List
from .database import Database

class NotesRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()
        self._init_table()

    def _init_table(self):
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL, 
                chat_id INTEGER
            )
        """)
        conn.commit()
        conn.close()

    def add(self,chat_id: int, text: str) -> None:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO notes (chat_id, text) VALUES (?,?)", (chat_id, text,))
        
        conn.commit()
        conn.close()

    def list(self, chat_id: int) -> List[str]:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT text FROM notes where chat_id = ? ORDER BY id DESC", (chat_id,))
        
        result = [row[0] for row in cursor.fetchall()]
        
        conn.close()
        
        return result