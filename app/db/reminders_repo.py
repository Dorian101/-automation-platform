from .database import Database
from datetime import datetime
from typing import List, Tuple


class RemindersRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()


    def add(self,chat_id:int, text: str, remind_at: str) -> None:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute(
        """
        INSERT INTO reminders (chat_id, text, remind_at)
        VALUES (%s, %s, %s)
        """,
        (chat_id, text, remind_at),
        )
        
        conn.commit()
        conn.close()

    def get_due(self) -> List[Tuple[int, str]]:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute(
        """
        SELECT id, chat_id, text
        FROM reminders
        WHERE is_sent = FALSE AND remind_at <= %s
        """,
        (datetime.utcnow(),),
        )
        
        result = cursor.fetchall()
        conn.close()
        
        return result

    def mark_sent(self, reminder_id: int) -> None:
        conn = self.database.connect()
        cursor = conn.cursor()
        cursor.execute(
        """
        UPDATE reminders
        SET is_sent = TRUE
        WHERE id = %s
        """,
        (reminder_id,),
        )
        conn.commit()
        conn.close()