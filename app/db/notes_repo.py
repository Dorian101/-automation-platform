from typing import List
from .database import Database

class NotesRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    

    def add(self,chat_id: int, text: str) -> None:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO notes (chat_id, text)
            VALUES (%s, %s)
            """,
            (chat_id, text),
        )
        
        conn.commit()
        conn.close()

    def list(self, chat_id: int) -> List[str]:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute(
        """
        SELECT text
        FROM notes
        WHERE chat_id = %s
        ORDER BY id DESC
        """,
        (chat_id,),
        )
        
        result = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        
        return result