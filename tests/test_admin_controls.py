"""What the administrator can change and read without SSH."""

import logging
import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.identity import Identity, guest
from app.core.results import CommandError
from app.db import errors_repo
from app.db.errors_repo import ErrorLogHandler, ErrorsRepository
from app.db.links_repo import TelegramLinksRepository
from app.db.sessions_repo import SessionsRepository
from app.db.settings_repo import SettingsRepository
from app.plugins.todo import TodoPlugin
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def deliver(self, target, text):
        self.sent.append((target, text))
        return True


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def server(manager, database_with_schema, web_server_factory, notifier):
    return web_server_factory(manager, database_with_schema, notifier=notifier)


@pytest.fixture
async def client(server):
    test_client = TestClient(TestServer(server.build_app()))
    await test_client.start_server()
    yield test_client
    await test_client.close()


@pytest.fixture
async def admin(client, login, create_user, users):
    account = users.get_by_username(TEST_USERNAME)
    users.set_admin(account.id, True)
    await login(client)
    return client


async def _token(client, path="/admin/settings") -> str:
    body = await (await client.get(path)).text()
    return re.search(r'name="csrf" value="([^"]+)"', body).group(1)


async def _save(client, **fields):
    data = {"csrf": await _token(client), "feedback_per_hour": "3",
            "demo_per_hour": "200", "plugin": ["notes", "snippets"]}
    data.update(fields)
    form = []
    for key, value in data.items():
        for item in value if isinstance(value, list) else [value]:
            form.append((key, item))
    return await client.post("/admin/settings", data=form, allow_redirects=False)


class TestSettings:
    async def test_only_for_administrators(self, client):
        response = await client.get("/admin/settings", allow_redirects=False)

        assert response.status == 303

    async def test_a_plugin_switched_off(self, admin, manager):
        response = await _save(admin, plugin=["notes"])

        assert response.headers["Location"] == "/admin/settings?saved=1"
        with pytest.raises(CommandError, match="временно отключена"):
            await manager.execute("/snippets", "", Identity("web", TEST_USERNAME))
        assert "snippets" not in [entry["name"] for entry in manager.pages()]

    async def test_a_forged_form_changes_nothing(self, admin, database_with_schema):
        await admin.post(
            "/admin/settings", data={"csrf": "made-up", "maintenance": "1"},
            allow_redirects=False,
        )

        assert SettingsRepository(database_with_schema).maintenance() is False

    async def test_a_limit_out_of_range_is_ignored(self, admin, server):
        await _save(admin, feedback_per_hour="5")
        await _save(admin, feedback_per_hour="100000")

        assert server._settings.feedback_per_hour() == 5

    async def test_the_feedback_limit_applies(self, admin):
        await _save(admin, feedback_per_hour="1")

        first = await admin.post("/feedback", json={"text": "раз"})
        second = await admin.post("/feedback", json={"text": "два"})

        assert (first.status, second.status) == (200, 429)

    async def test_registration_can_be_opened(self, admin, client):
        await _save(admin, signup="1")
        await admin.post("/logout", data={"csrf": await _token(admin)})

        assert (await client.get("/signup")).status == 200


class TestMaintenance:
    async def test_visitors_wait_and_administrators_work(
        self, admin, server, manager, database_with_schema, web_server_factory
    ):
        await _save(admin, maintenance="1")

        assert (await admin.get("/about")).status == 200

        async with TestClient(TestServer(server.build_app())) as stranger:
            page = await stranger.get("/about")
            assert page.status == 503
            assert page.headers["Retry-After"] == "600"
            assert (await stranger.get("/login")).status == 200

        with pytest.raises(CommandError, match="обслуживании"):
            await manager.execute("/notes", "", Identity("web", TEST_USERNAME))


class TestErrors:
    def test_the_handler_keeps_the_newest(self, database_with_schema, monkeypatch):
        monkeypatch.setattr(errors_repo, "KEEP", 2)
        log = logging.getLogger("test.errors")
        handler = ErrorLogHandler(ErrorsRepository(database_with_schema))
        log.addHandler(handler)
        try:
            for number in range(3):
                log.error("failure %s", number)
            log.warning("not an error")
        finally:
            log.removeHandler(handler)

        found = ErrorsRepository(database_with_schema).recent()

        assert [error.message for error in found] == ["failure 2", "failure 1"]

    async def test_the_page_escapes(self, admin, database_with_schema):
        ErrorsRepository(database_with_schema).add("app", "<b>boom</b>", "")

        body = await (await admin.get("/admin/errors")).text()

        assert "&lt;b&gt;boom&lt;/b&gt;" in body


class TestBroadcast:
    async def test_every_linked_active_account_gets_it(
        self, admin, users, create_user, notifier, database_with_schema
    ):
        links = TelegramLinksRepository(database_with_schema)
        links.link(users.get_by_username(TEST_USERNAME).id, 111)
        off = create_user(username="gone", password=TEST_PASSWORD)
        links.link(off.id, 222)
        users.set_active(off.id, False)

        response = await admin.post(
            "/admin/broadcast",
            data={"csrf": await _token(admin, "/admin/broadcast"), "text": "Привет"},
            allow_redirects=False,
        )

        assert response.headers["Location"] == "/admin/broadcast?sent=1&of=1"
        assert notifier.sent == [(Identity("telegram", "111"), "Привет")]


class TestTasks:
    async def test_guest_cleanup_takes_tasks_too(
        self, admin, manager, database_with_schema
    ):
        """`tasks` was missing from the cleanup, so guests' to-dos stayed."""
        todo = TodoPlugin(None, database_with_schema)
        await todo.seed_demo(guest("abc"))

        response = await admin.post(
            "/admin/tasks/guests",
            data={"csrf": await _token(admin, "/admin/status")},
            allow_redirects=False,
        )

        assert response.headers["Location"].startswith("/admin/status?done=guests")
        with database_with_schema.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tasks WHERE user_id LIKE 'web:guest:%'")
            assert cur.fetchone()[0] == 0

    async def test_backup_now(self, admin, monkeypatch):
        made = []
        monkeypatch.setattr(
            "app.web.admin.BackupManager.create_backup",
            lambda self, name: made.append(name),
        )

        await admin.post(
            "/admin/tasks/backup",
            data={"csrf": await _token(admin, "/admin/status")},
        )

        assert made == ["manual"]

    async def test_the_demo_block_counts_guests(self, admin, database_with_schema):
        await TodoPlugin(None, database_with_schema).seed_demo(guest("abc"))

        body = await (await admin.get("/admin/status")).text()

        assert "Гостей сейчас: <b>1</b>" in body


class TestLogins:
    async def test_a_session_can_be_ended(self, admin, users, database_with_schema):
        sessions = SessionsRepository(database_with_schema)
        account = users.get_by_username(TEST_USERNAME)
        sessions.create(account.id, 3600, agent="iPhone · Safari")
        other = next(s for s in sessions.active() if s.agent == "iPhone · Safari")

        body = await (await admin.get("/admin/users")).text()
        assert "это вы" in body and "iPhone · Safari" in body

        await admin.post(
            f"/admin/logins/{other.id}/end",
            data={"csrf": await _token(admin, "/admin/users")},
        )

        assert other.id not in [s.id for s in sessions.active()]
        assert (await admin.get("/admin/users")).status == 200


def test_a_database_outage_pauses_the_error_log(monkeypatch):
    """A failed write must not make every following error wait on the pool."""
    calls = []

    class Down:
        def add(self, *args):
            calls.append(args)
            raise ConnectionError("database is down")

    handler = ErrorLogHandler(Down())
    record = logging.LogRecord("x", logging.ERROR, "", 0, "boom", None, None)

    handler.emit(record)
    handler.emit(record)

    assert len(calls) == 1


class TestActiveMeansInUse:
    def _age(self, database, session_id, minutes):
        with database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sessions SET created_at = CURRENT_TIMESTAMP - "
                    "make_interval(mins => %s), last_seen = NULL WHERE id = %s",
                    (minutes, session_id),
                )
            conn.commit()

    def test_an_idle_sign_in_is_not_listed(
        self, users, create_user, database_with_schema
    ):
        sessions = SessionsRepository(database_with_schema)
        account = users.get_by_username(TEST_USERNAME)
        sessions.create(account.id, 86400, agent="old")
        old = sessions.active()[0]
        self._age(database_with_schema, old.id, 120)

        assert sessions.active() == []

    def test_using_it_brings_it_back(self, users, create_user, database_with_schema):
        sessions = SessionsRepository(database_with_schema)
        account = users.get_by_username(TEST_USERNAME)
        token = sessions.create(account.id, 86400)
        self._age(database_with_schema, sessions.active()[0].id, 120)

        assert sessions.get_user_id(token) == account.id
        assert len(sessions.active()) == 1


class TestTelegramOutages:
    def _failures(self, handler, *seconds):
        for at in seconds:
            record = logging.LogRecord(
                "aiogram.dispatcher",
                logging.ERROR,
                "",
                0,
                "Failed to fetch updates - TelegramServerError: Bad Gateway",
                None,
                None,
            )
            record.created = at
            handler.emit(record)

    def test_a_short_outage_is_not_stored(self, database_with_schema):
        repository = ErrorsRepository(database_with_schema)
        handler = ErrorLogHandler(repository)

        self._failures(handler, 0, 1, 3, 6, 60)

        assert repository.recent() == []

    def test_a_long_outage_is_stored_once(self, database_with_schema):
        repository = ErrorsRepository(database_with_schema)
        handler = ErrorLogHandler(repository)

        self._failures(handler, *range(0, 400, 5))

        [error] = repository.recent()
        assert error.message.startswith("Нет связи с Telegram дольше 5 минут")

    def test_a_gap_starts_a_new_outage(self, database_with_schema):
        repository = ErrorsRepository(database_with_schema)
        handler = ErrorLogHandler(repository)

        self._failures(handler, 0, 200, 400, 600)

        assert repository.recent() == []

    def test_other_errors_are_still_stored(self, database_with_schema):
        repository = ErrorsRepository(database_with_schema)
        handler = ErrorLogHandler(repository)
        record = logging.LogRecord(
            "aiogram.dispatcher", logging.ERROR, "", 0, "Cause exception", None, None
        )

        handler.emit(record)

        assert [error.message for error in repository.recent()] == ["Cause exception"]
