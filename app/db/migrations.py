import logging
from pathlib import Path

from app.db.database import Database


class MigrationRunner:
    """Applies pending SQL migrations."""

    def __init__(self, database: Database):
        self.logger = logging.getLogger(__name__)

        self.database = database
        self.project_root = Path(__file__).resolve().parents[2]
        self.migrations_dir = self.project_root / "sql" / "migrations"

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

        applied_count = 0

        for migration in migrations:
            if migration.name in applied:
                continue

            self.logger.info("Applying migration: %s", migration.name)
            self.apply_migration(migration)

            applied_count += 1

            self.logger.info("Migration applied: %s", migration.name)

        if applied_count:
            self.logger.info(
                "Applied %s new migration(s)",
                applied_count,
            )
        else:
            self.logger.info("Database schema is up to date")

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
