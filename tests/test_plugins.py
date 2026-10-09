from datetime import UTC, datetime, timedelta

import pytest

from app.core.identity import TELEGRAM, WEB, Identity, telegram
from app.core.results import CommandError
from app.plugins.clipboard import ClipboardPlugin
from app.plugins.notes import NotesPlugin
from app.plugins.reminders import MAX_DROPPED_ATTEMPTS, RemindersPlugin
from app.plugins.system import SystemPlugin
from tests.conftest import TEST_USERNAME


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


class RecordingNotifier:
    """Reports delivery the way the real Notifier does.

    The two failure modes are separate on purpose: raising is an outage and
    should be retried for ever, returning False is nowhere-to-deliver and must
    eventually be given up on.
    """

    def __init__(self, delivered: bool = True, raises: Exception | None = None):
        self.delivered = delivered
        self.raises = raises
        self.calls: list[tuple[Identity, str]] = []

    async def deliver(self, target, text):
        self.calls.append((target, text))

        if self.raises is not None:
            raise self.raises

        return self.delivered

    def kinds(self):
        return ["telegram"]


class TestReminderDelivery:
    async def test_a_delivered_reminder_is_marked_sent(
        self,
        database_with_schema,
        web_identity,
    ):
        notifier = RecordingNotifier()
        plugin = RemindersPlugin(notifier, database_with_schema)
        plugin.repo.add(web_identity, "call mum", _one_minute_ago())

        await plugin.process_due()

        assert plugin.repo.get_due() == []

    async def test_a_raising_delivery_keeps_the_reminder_due(
        self,
        database_with_schema,
        web_identity,
    ):
        notifier = RecordingNotifier(raises=RuntimeError("telegram is down"))
        plugin = RemindersPlugin(notifier, database_with_schema)
        plugin.repo.add(web_identity, "call mum", _one_minute_ago())

        await plugin.process_due()

        assert len(plugin.repo.get_due()) == 1

    async def test_an_outage_is_never_counted_as_a_drop(
        self,
        database_with_schema,
        web_identity,
    ):
        """A brief Telegram failure must not eat the reminder permanently."""
        notifier = RecordingNotifier(raises=RuntimeError("telegram is down"))
        plugin = RemindersPlugin(notifier, database_with_schema)
        plugin.repo.add(web_identity, "call mum", _one_minute_ago())

        for _ in range(MAX_DROPPED_ATTEMPTS * 2):
            await plugin.process_due()

        assert len(plugin.repo.get_due()) == 1

    async def test_nothing_to_deliver_is_given_up_on_eventually(
        self,
        database_with_schema,
        web_identity,
    ):
        """An unlinked account must not fill the journal for ever."""
        notifier = RecordingNotifier(delivered=False)
        plugin = RemindersPlugin(notifier, database_with_schema)
        plugin.repo.add(web_identity, "call mum", _one_minute_ago())

        for _ in range(MAX_DROPPED_ATTEMPTS - 1):
            await plugin.process_due()
            assert len(plugin.repo.get_due()) == 1

        await plugin.process_due()

        assert plugin.repo.get_due() == []

    async def test_giving_up_says_so(
        self,
        database_with_schema,
        web_identity,
        caplog,
    ):
        notifier = RecordingNotifier(delivered=False)
        plugin = RemindersPlugin(notifier, database_with_schema)
        plugin.repo.add(web_identity, "call mum", _one_minute_ago())

        with caplog.at_level("WARNING"):
            for _ in range(MAX_DROPPED_ATTEMPTS):
                await plugin.process_due()

        assert "Giving up" in caplog.text

    async def test_linking_in_time_still_delivers(
        self,
        database_with_schema,
        web_identity,
    ):
        """The common real sequence: set a reminder, link Telegram a moment
        later. Giving up before that would lose the reminder."""
        notifier = RecordingNotifier(delivered=False)
        plugin = RemindersPlugin(notifier, database_with_schema)
        plugin.repo.add(web_identity, "call mum", _one_minute_ago())

        for _ in range(MAX_DROPPED_ATTEMPTS - 1):
            await plugin.process_due()

        notifier.delivered = True
        await plugin.process_due()

        assert plugin.repo.get_due() == []

    async def test_a_delivered_reminder_forgets_its_drop_count(
        self,
        database_with_schema,
        web_identity,
    ):
        """Otherwise the plugin carries an entry for every reminder ever sent."""
        notifier = RecordingNotifier()
        plugin = RemindersPlugin(notifier, database_with_schema)
        plugin.repo.add(web_identity, "call mum", _one_minute_ago())
        plugin._dropped_attempts[1] = 2

        await plugin.process_due()

        assert plugin._dropped_attempts == {}

    async def test_an_unparsable_stored_identity_is_still_skipped(
        self,
        database_with_schema,
        notifier,
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

        await plugin.process_due()

        assert len(plugin.repo.get_due()) == 1

    async def test_one_undeliverable_reminder_does_not_block_the_others(
        self,
        database_with_schema,
        web_identity,
    ):
        """A single unlinked account must not hold up everyone else's."""
        class PerTarget(RecordingNotifier):
            """Delivers only to Telegram, so the web one has nowhere to go."""

            async def deliver(self, target, text):
                self.calls.append((target, text))
                return target.kind == TELEGRAM

        plugin = RemindersPlugin(PerTarget(), database_with_schema)

        plugin.repo.add(web_identity, "web reminder", _one_minute_ago())
        plugin.repo.add(telegram(42), "bot reminder", _one_minute_ago())

        await plugin.process_due()

        remaining = [reminder for reminder in plugin.repo.get_due()]

        assert [reminder[2] for reminder in remaining] == ["web reminder"]


class TestStatusSaysWhereRemindersGo:
    """A reminder that quietly goes nowhere is invisible until it is missed."""

    def _plugin(self, manager, users, links):
        return SystemPlugin(manager, users=users, links=links)

    @pytest.fixture
    def account_identity(self):
        """The web identity of the real default account.

        `web_identity` is a bare `web:default`, which no row matches — right
        for testing command dispatch, wrong for anything that looks an account
        up the way /status does.
        """
        return Identity(kind=WEB, id=TEST_USERNAME)

    async def test_an_unlinked_web_user_is_told(
        self,
        manager,
        database_with_schema,
        create_user,
        account_identity,
    ):
        from app.db.links_repo import TelegramLinksRepository
        from app.db.users_repo import UsersRepository

        users = UsersRepository(database_with_schema)
        links = TelegramLinksRepository(database_with_schema)
        plugin = self._plugin(manager, users, links)

        result = await plugin.execute("/status", "", account_identity)

        assert "link your Telegram account" in result.text

    async def test_a_linked_web_user_is_told_where(
        self,
        manager,
        database_with_schema,
        create_user,
        account_identity,
    ):
        from app.db.links_repo import TelegramLinksRepository
        from app.db.users_repo import UsersRepository

        users = UsersRepository(database_with_schema)
        links = TelegramLinksRepository(database_with_schema)
        links.link(users.get_by_username(TEST_USERNAME).id, 4242)
        plugin = self._plugin(manager, users, links)

        result = await plugin.execute("/status", "", account_identity)

        assert "Telegram chat 4242" in result.text

    async def test_a_telegram_user_is_told_nothing_is_wrong(
        self,
        manager,
    ):
        result = await SystemPlugin(manager).execute("/status", "", telegram(42))

        assert "Notifications: Telegram" in result.text

    async def test_it_still_works_without_a_database(
        self,
        manager,
        account_identity,
    ):
        """/status must not depend on wiring it was not built with."""
        result = await SystemPlugin(manager).execute("/status", "", account_identity)

        assert "Plugins loaded" in result.text
        assert "no Telegram link available" in result.text
