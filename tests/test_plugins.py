from datetime import UTC, datetime, timedelta

import pytest

from app.core.identity import WEB, Identity, telegram
from app.core.results import CommandError
from app.plugins.clipboard import ClipboardPlugin
from app.plugins.notes import NotesPlugin
from app.plugins.reminders import RemindersPlugin
from app.plugins.system import SystemPlugin


def _one_minute_ago() -> str:
    return (
        datetime.now(UTC) - timedelta(minutes=1)
    ).replace(tzinfo=None).isoformat()


class TestNotesPlugin:
    async def test_add_then_list(self, database_with_schema, web_identity):
        plugin = NotesPlugin(database_with_schema)

        saved = await plugin.execute("/add", "first note", web_identity)
        assert saved.text == "Saved"

        listed = await plugin.execute("/notes", "", web_identity)
        assert listed.text == "- first note"

    async def test_empty_args_rejected(self, database_with_schema, web_identity):
        plugin = NotesPlugin(database_with_schema)

        with pytest.raises(CommandError, match="Usage"):
            await plugin.execute("/add", "", web_identity)

    async def test_empty_list(self, database_with_schema, web_identity):
        plugin = NotesPlugin(database_with_schema)

        result = await plugin.execute("/notes", "", web_identity)

        assert result.text == "No notes"

    async def test_transports_are_isolated(self, database_with_schema):
        plugin = NotesPlugin(database_with_schema)
        web_user = Identity(kind=WEB, id="default")
        tg_user = telegram(777)

        await plugin.execute("/add", "from web", web_user)
        await plugin.execute("/add", "from telegram", tg_user)

        web_result = await plugin.execute("/notes", "", web_user)
        tg_result = await plugin.execute("/notes", "", tg_user)

        assert web_result.text == "- from web"
        assert tg_result.text == "- from telegram"

    async def test_same_numeric_id_different_transport(self, database_with_schema):
        plugin = NotesPlugin(database_with_schema)
        web_user = Identity(kind=WEB, id="777")
        tg_user = telegram(777)

        await plugin.execute("/add", "web note", web_user)
        await plugin.execute("/add", "telegram note", tg_user)

        assert (await plugin.execute("/notes", "", web_user)).text == "- web note"
        assert (await plugin.execute("/notes", "", tg_user)).text == "- telegram note"


class TestClipboardPlugin:
    async def test_copy_then_paste(self, database_with_schema, web_identity):
        plugin = ClipboardPlugin(database_with_schema)

        await plugin.execute("/copy", "copied text", web_identity)
        result = await plugin.execute("/paste", "", web_identity)

        assert result.text == "copied text"

    async def test_empty_clipboard(self, database_with_schema, web_identity):
        plugin = ClipboardPlugin(database_with_schema)

        result = await plugin.execute("/paste", "", web_identity)

        assert result.text == "Clipboard is empty"

    async def test_empty_args_rejected(self, database_with_schema, web_identity):
        plugin = ClipboardPlugin(database_with_schema)

        with pytest.raises(CommandError, match="Usage"):
            await plugin.execute("/copy", "", web_identity)

    async def test_paste_isolated_by_transport(self, database_with_schema):
        plugin = ClipboardPlugin(database_with_schema)
        web_user = Identity(kind=WEB, id="default")

        await plugin.execute("/copy", "web clipboard", web_user)
        result = await plugin.execute("/paste", "", telegram(777))

        assert result.text == "Clipboard is empty"


class TestRemindersPlugin:
    async def test_creates_reminder(self, database_with_schema, web_identity, notifier):
        plugin = RemindersPlugin(notifier, database_with_schema)

        result = await plugin.execute(
            "/remind",
            "5 call mum",
            web_identity,
        )

        assert result.text == "Reminder set in 5 min"

        due = database_with_schema and plugin.repo.get_due()
        assert len(due) == 0

    async def test_past_reminder_is_due(
        self, database_with_schema, web_identity, notifier,
    ):
        plugin = RemindersPlugin(notifier, database_with_schema)

        plugin.repo.add(web_identity, "overdue", _one_minute_ago())

        due = plugin.repo.get_due()
        assert len(due) == 1

    async def test_missing_text_rejected(
        self, database_with_schema, web_identity, notifier,
    ):
        plugin = RemindersPlugin(notifier, database_with_schema)

        with pytest.raises(CommandError, match="Usage"):
            await plugin.execute("/remind", "5", web_identity)

    async def test_non_numeric_minutes_rejected(
        self, database_with_schema, web_identity, notifier,
    ):
        plugin = RemindersPlugin(notifier, database_with_schema)

        with pytest.raises(CommandError, match="number"):
            await plugin.execute("/remind", "abc hello", web_identity)

    async def test_negative_minutes_rejected(
        self, database_with_schema, web_identity, notifier,
    ):
        plugin = RemindersPlugin(notifier, database_with_schema)

        with pytest.raises(CommandError, match="negative"):
            await plugin.execute("/remind", "-5 hello", web_identity)

    async def test_stores_qualified_user_id(
        self, database_with_schema, web_identity, notifier,
    ):
        plugin = RemindersPlugin(notifier, database_with_schema)

        plugin.repo.add(web_identity, "check", _one_minute_ago())

        due = plugin.repo.get_due()
        assert due[0][1] == "web:default"

    async def test_unparsable_user_id_is_skipped(
        self, database_with_schema, web_identity, notifier,
    ):
        plugin = RemindersPlugin(notifier, database_with_schema)

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO reminders (user_id, text, remind_at)
                    VALUES (%s, %s, %s::timestamp)
                    """,
                    ("garbage", "bad", _one_minute_ago()),
                )
            conn.commit()

        due = plugin.repo.get_due()
        assert len(due) == 1
        assert due[0][1] == "garbage"


class TestSystemPlugin:
    async def test_help_lists_commands(self, manager, web_identity):
        plugin = SystemPlugin(manager)

        result = await plugin.execute("/help", "", web_identity)

        assert "/add" in result.text
        assert "/copy" in result.text

    async def test_plugins_lists_registered(self, manager, web_identity):
        plugin = SystemPlugin(manager)

        result = await plugin.execute("/plugins", "", web_identity)

        assert "notes" in result.text

    async def test_status_reports_count(self, manager, web_identity):
        plugin = SystemPlugin(manager)

        result = await plugin.execute("/status", "", web_identity)

        assert "Plugins loaded: 2" in result.text
