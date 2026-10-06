import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from app.core.config import Config

# The dump contains scrypt password hashes, so it must not be readable by the
# other accounts on the host. The mode is applied by os.open at creation time;
# going through pg_dump -f instead would create the file at the umask default
# and only tighten it afterwards.
_DUMP_MODE = 0o600


def default_backups_dir() -> Path:
    if Config.BACKUP_DIR:
        return Path(Config.BACKUP_DIR)

    # <repo>/backups. Fine for local development; on a server BACKUP_DIR
    # should point outside the working tree so the backups cannot be swept
    # away along with the checkout.
    return Path(__file__).resolve().parents[2] / "backups"


class BackupManager:
    """Creates and prunes PostgreSQL backups."""

    def __init__(self, backups_dir: Path | None = None):
        self.logger = logging.getLogger(__name__)
        self.backups_dir = (
            backups_dir if backups_dir is not None else default_backups_dir()
        )

    def create_backup(self, name: str) -> Path:
        """Dump the database to <backups_dir>/<name>_<timestamp>.sql."""
        self.backups_dir.mkdir(parents=True, exist_ok=True)

        path = self.backups_dir / (
            f"{name}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.sql"
        )

        try:
            handle = os.fdopen(
                os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _DUMP_MODE),
                "wb",
            )
        except FileExistsError:
            raise RuntimeError(
                f"a backup already exists for this second: {path.name}"
            ) from None

        # pg_dump writes to the file opened above. Passing -f would let it
        # create the file itself, which skips the mode and lets two runs in
        # the same second contend over one path.
        try:
            with handle:
                result = subprocess.run(
                    [
                        "pg_dump",
                        "-h", Config.DB_HOST,
                        "-p", str(Config.DB_PORT),
                        "-U", Config.DB_USER,
                        "-d", Config.DB_NAME,
                    ],
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    env={**os.environ, "PGPASSWORD": Config.DB_PASSWORD},
                )
        except OSError as error:
            # pg_dump is not installed. Worth turning into a sentence: this
            # is a first-deploy mistake, and the journal is where it will be
            # read. The half-written file goes too.
            path.unlink()
            raise RuntimeError(f"could not run pg_dump: {error}") from error

        if result.returncode != 0:
            path.unlink()
            raise RuntimeError(
                f"backup failed: {result.stderr.decode().strip()}"
            )

        self.logger.info("Backup created: %s", path.name)
        return path

    def prune(self, retention_days: int) -> list[Path]:
        """Delete backups older than retention_days, returning what went.

        Zero or less disables pruning. "Keep everything" is the safer reading
        of a misconfigured retention than "delete everything at once", and it
        makes an unset value harmless.
        """
        if retention_days <= 0:
            return []

        if not self.backups_dir.is_dir():
            return []

        cutoff = time.time() - retention_days * 86400
        removed: list[Path] = []

        for path in sorted(self.backups_dir.glob("*.sql")):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
                self.logger.info("Pruned backup: %s", path.name)

        return removed
