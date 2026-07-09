from pathlib import Path
import sqlite3


class Database:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            project_root = Path(__file__).resolve().parents[2]
            db_path = project_root / "data" / "app.db"

        self.db_path = str(db_path)

    def connect(self):
        return sqlite3.connect(self.db_path)