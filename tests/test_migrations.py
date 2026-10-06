from pathlib import Path

from app.db.migrations import MigrationRunner

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "sql" / "migrations"


def _migration_names() -> list[str]:
    return sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))


class TestMigrations:
    def test_creates_schema_migrations_table(self, database):
        MigrationRunner(database).run()

        with database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = 'schema_migrations'
                    )
                """)
                assert cur.fetchone()[0] is True

    def test_applies_all_migrations(self, database):
        MigrationRunner(database).run()

        with database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM schema_migrations ORDER BY version")
                names = [row[0] for row in cur.fetchall()]

        assert names == _migration_names()

    def test_is_idempotent(self, database):
        runner = MigrationRunner(database)
        runner.run()
        runner.run()
        runner.run()

        with database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM schema_migrations")
                assert cur.fetchone()[0] == len(_migration_names())

    def test_creates_all_tables(self, database):
        MigrationRunner(database).run()

        with database.connect() as conn:
            with conn.cursor() as cur:
                for table in ("notes", "reminders", "clipboard", "users", "sessions"):
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables
                            WHERE table_name = %s
                        )
                    """, (table,))
                    assert cur.fetchone()[0] is True, f"Table {table} missing"


class TestUserIdentityMigration:
    def test_renames_chat_id_to_user_id(self, database):
        MigrationRunner(database).run()

        with database.connect() as conn:
            with conn.cursor() as cur:
                for table in ("notes", "reminders", "clipboard"):
                    cur.execute("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name = %s AND column_name = 'user_id'
                    """, (table,))
                    assert cur.fetchone() is not None, f"{table}.user_id missing"

                    cur.execute("""
                        SELECT COUNT(*) FROM information_schema.columns
                        WHERE table_name = %s AND column_name = 'chat_id'
                    """, (table,))
                    assert cur.fetchone()[0] == 0, f"{table}.chat_id still present"

    def test_user_id_is_text(self, database):
        MigrationRunner(database).run()

        with database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT data_type FROM information_schema.columns
                    WHERE table_name = 'notes' AND column_name = 'user_id'
                """)
                assert cur.fetchone()[0] == "text"

    def test_backfills_existing_rows_with_namespace(self, database):
        with database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE notes (
                        id BIGSERIAL PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        text TEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("""
                    CREATE TABLE reminders (
                        id BIGSERIAL PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        text TEXT NOT NULL,
                        remind_at TIMESTAMP NOT NULL,
                        is_sent BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("""
                    CREATE TABLE clipboard (
                        id BIGSERIAL PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        text TEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute(
                    "INSERT INTO notes (chat_id, text) VALUES (%s, %s)",
                    (-1001234567890, "legacy note"),
                )
                cur.execute("""
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (1, "001_initial.sql"),
                )
            conn.commit()

        MigrationRunner(database).run()

        with database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, text FROM notes")
                rows = cur.fetchall()

        assert rows == [("telegram:-1001234567890", "legacy note")]

    def test_creates_user_id_indexes(self, database):
        MigrationRunner(database).run()

        with database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT indexname FROM pg_indexes
                    WHERE tablename IN ('notes', 'reminders', 'clipboard')
                        AND indexname LIKE '%user_id%'
                """)
                indexes = {row[0] for row in cur.fetchall()}

        assert indexes == {
            "idx_notes_user_id",
            "idx_reminders_user_id",
            "idx_clipboard_user_id",
        }
