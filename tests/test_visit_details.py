"""Telling visitors apart without keeping who they are, and the sessions view."""

from datetime import datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db.visits_repo import Visit, VisitsRepository
from app.web import visitor
from app.web.admin import SESSION_GAP_MINUTES, _sessions
from tests.conftest import TEST_USERNAME, with_passkey

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
WINDOWS_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
TELEGRAM_IN_APP = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like "
    "Gecko) Chrome/130.0 Mobile Safari/537.36 Telegram-Android/11.2.1"
)


class TestDescribe:
    @pytest.mark.parametrize(
        ("agent", "label"),
        [
            (IPHONE, "iPhone · Safari"),
            (WINDOWS_CHROME, "Windows · Chrome"),
            (WINDOWS_CHROME + " Edg/130.0", "Windows · Edge"),
            (WINDOWS_CHROME + " YaBrowser/24.1", "Windows · Яндекс Браузер"),
            ("", "—"),
        ],
    )
    def test_labels(self, agent, label):
        assert visitor.describe(agent) == label

    @pytest.mark.parametrize(
        "agent",
        [
            "TelegramBot (like TwitterBot)",
            "Mozilla/5.0 (compatible; Googlebot/2.1)",
            "Mozilla/5.0 (compatible; YandexBot/3.0)",
            "WhatsApp/2.24.1 A",
            "curl/8.4.0",
            "python-requests/2.32",
            "",
        ],
    )
    def test_robots(self, agent):
        assert visitor.is_bot(agent)

    @pytest.mark.parametrize("agent", [IPHONE, WINDOWS_CHROME, TELEGRAM_IN_APP])
    def test_people(self, agent):
        """Telegram's in-app browser is somebody reading, not a preview."""
        assert not visitor.is_bot(agent)


class TestFingerprint:
    def test_same_browser_same_day(self):
        assert visitor.fingerprint(b"s", "192.0.2.0", IPHONE) == visitor.fingerprint(
            b"s", "192.0.2.0", IPHONE
        )

    def test_another_day_another_hash(self):
        assert visitor.fingerprint(b"monday", "192.0.2.0", IPHONE) != (
            visitor.fingerprint(b"tuesday", "192.0.2.0", IPHONE)
        )

    def test_another_browser_another_hash(self):
        assert visitor.fingerprint(b"s", "192.0.2.0", IPHONE) != visitor.fingerprint(
            b"s", "192.0.2.0", WINDOWS_CHROME
        )

    def test_it_is_short_and_carries_no_address(self):
        value = visitor.fingerprint(b"s", "192.0.2.0", IPHONE)

        assert len(value) == visitor.FINGERPRINT_LENGTH
        assert "192.0.2.0" not in value


class TestSalt:
    def test_one_salt_per_day_and_yesterdays_is_gone(self, database_with_schema):
        repo = VisitsRepository(database_with_schema)
        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO visit_salts (day, salt) VALUES "
                    "(CURRENT_DATE - 1, 'old')"
                )
            conn.commit()

        first = repo.daily_salt()

        assert VisitsRepository(database_with_schema).daily_salt() == first
        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM visit_salts")
                assert cur.fetchone()[0] == 1


@pytest.fixture
async def client(manager, database_with_schema, web_server_factory):
    server = web_server_factory(manager, database_with_schema)
    test_client = TestClient(TestServer(server.build_app()))
    await test_client.start_server()
    yield test_client
    await test_client.close()


def _log(database):
    return VisitsRepository(database).recent()


class TestRecording:
    async def test_a_visit_carries_its_details(self, client, database_with_schema):
        await client.get("/about?from=HH", headers={"User-Agent": IPHONE})

        row = _log(database_with_schema)[0]

        assert row.agent == "iPhone · Safari"
        assert row.bot is False
        assert row.campaign == "hh"
        assert len(row.visitor) == visitor.FINGERPRINT_LENGTH

    async def test_one_browser_one_hash(self, client, database_with_schema):
        await client.get("/about", headers={"User-Agent": IPHONE})
        await client.get("/project", headers={"User-Agent": IPHONE})
        await client.get("/about", headers={"User-Agent": WINDOWS_CHROME})

        hashes = [row.visitor for row in _log(database_with_schema)]

        assert hashes[1] == hashes[2] != hashes[0]

    async def test_a_preview_is_marked(self, client, database_with_schema):
        await client.get(
            "/about", headers={"User-Agent": "TelegramBot (like TwitterBot)"}
        )

        assert _log(database_with_schema)[0].bot is True

    async def test_a_strange_tag_is_ignored(self, client, database_with_schema):
        await client.get("/about?from=<script>", headers={"User-Agent": IPHONE})

        assert _log(database_with_schema)[0].campaign is None


def _visit(minute, who="a", path="/about", bot=False, identity=None):
    return Visit(
        at=datetime(2026, 11, 3, 10, 0) + timedelta(minutes=minute),
        kind="view", path=path, identity=identity, visitor=who, bot=bot,
    )


class TestGrouping:
    def test_a_long_pause_starts_a_new_session(self):
        found = _sessions([
            _visit(0), _visit(5), _visit(5 + SESSION_GAP_MINUTES + 1),
        ])

        assert [len(session.visits) for session in found] == [2, 1]

    def test_two_browsers_at_once_stay_apart(self):
        found = _sessions([_visit(0, "a"), _visit(1, "b"), _visit(2, "a")])

        assert [len(session.visits) for session in found] == [2, 1]


class TestTheSessionsPage:
    @pytest.fixture
    async def admin(self, client, login, create_user, users):
        account = users.get_by_username(TEST_USERNAME)
        users.set_admin(account.id, True)
        await login(client)
        with_passkey(client, users)
        return client

    async def test_bots_are_hidden_until_asked_for(self, admin):
        await admin.get("/about?from=hh", headers={"User-Agent": IPHONE})
        await admin.get("/project", headers={"User-Agent": IPHONE})
        await admin.get("/about", headers={"User-Agent": "curl/8.4.0"})

        body = await (await admin.get("/admin/sessions")).text()

        assert "/about &rarr; /project" in body
        assert '<span class="badge on">hh</span>' in body
        assert "бот</span>" not in body

        with_bots = await (await admin.get("/admin/sessions?bots=1")).text()

        assert "бот</span>" in with_bots

    async def test_it_is_for_administrators_only(self, client):
        response = await client.get("/admin/sessions", allow_redirects=False)

        assert response.status == 303
