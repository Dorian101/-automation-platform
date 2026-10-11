import logging
from pathlib import Path

from app.db.database import Database


class MigrationRunner:
    """Applies pending SQL migrations."""

    def __init__(self, database: Database, backup=None):
        self.logger = logging.getLogger(__name__)

        self.database = database
        self.project_root = Path(__file__).resolve().parents[2]
        self.migrations_dir = self.project_root / "sql" / "migrations"

        # Optional rather than constructed here. The production entry point
        # passes a BackupManager so a deploy that carries a migration leaves a
        # dump behind it; the tests and the management commands do not, and
        # making every `MigrationRunner` in the codebase shell out to pg_dump
        # would slow the suite down for a safety net none of them need.
        self._backup = backup

    def run(self) -> None:
        self.logger.info("Checking database migrations...")

        self.ensure_migrations_table()

        applied = self.get_applied_migrations()
        migrations = self.discover_migrations()

        self.logger.info(
            "Found %s migration files, %s already applied",
            len(migrations),
            len(applied),
        )

        pending = [m for m in migrations if m.name not in applied]

        if not pending:
            self.logger.info("Database schema is up to date")
            return

        self.backup_before(pending)

        for migration in pending:
            self.logger.info("Applying migration: %s", migration.name)
            self.apply_migration(migration)

            self.logger.info("Migration applied: %s", migration.name)

        self.logger.info("Applied %s new migration(s)", len(pending))

    def backup_before(self, pending: list[Path]) -> None:
        """Dump the database before changing the schema.

        One dump for the whole run rather than one per migration: a deploy
        brings several files, and what is wanted on a failure is a copy of
        the database as it was before any of them landed.

        Failing here stops the run. On a host where pg_dump works — which is
        this one, since the nightly backup uses it — a failure means something
        real: no disk, no database, a broken PATH. Applying a migration with
        no way back is the worse of the two mistakes, and the error will say
        what went wrong instead of surfacing later as lost rows.
        """
        if self._backup is None or not pending:
            return

        try:
            self._backup.create_backup("pre_migration")
        except Exception as error:
            raise RuntimeError(
                f"refusing to apply {len(pending)} migration(s) without a "
                f"backup: {error}"
            ) from error

    def ensure_migrations_table(self) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            conn.commit()

    def get_applied_migrations(self) -> set[str]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT name
                    FROM schema_migrations
                """)

                rows = cur.fetchall()

        return {row[0] for row in rows}

    def discover_migrations(self) -> list[Path]:
        """Return sorted migration files."""
        if not self.migrations_dir.exists():
            return []

        return sorted(self.migrations_dir.glob("*.sql"))

    def apply_migration(self, migration: Path) -> None:
        sql = migration.read_text(encoding="utf-8")

        version = int(migration.name.split("_")[0])

        with self.database.connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)

                    cur.execute(
                        """
                        INSERT INTO schema_migrations (
                            version,
                            name
                        )
                        VALUES (%s, %s)
                        """,
                        (version, migration.name),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
