import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from app.core.config import Config


class BackupManager:
    """Creates PostgreSQL backups."""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.project_root = Path(__file__).resolve().parents[2]
        self.backups_dir = self.project_root / "backups"

    def create_backup(self, migration_name: str) -> Path:
        self.backups_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        backup_path = self.backups_dir / (
            f"before_{migration_name.removesuffix('.sql')}_{timestamp}.sql"
        )

        result = subprocess.run(
            [
                "pg_dump",
                "-h", Config.DB_HOST,
                "-p", str(Config.DB_PORT),
                "-U", Config.DB_USER,
                "-d", Config.DB_NAME,
                "-f", str(backup_path),
            ],
            env={**os.environ, "PGPASSWORD": Config.DB_PASSWORD},
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Backup failed: {result.stderr}"
            )

        self.logger.info(
            "Backup created: %s",
            backup_path.name,
        )

        return backup_path
