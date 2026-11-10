import asyncio
import json
import logging
import re
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.identity import TELEGRAM, WEB, Identity
from app.db.links_repo import TelegramLinksRepository
from app.db.sessions_repo import SessionsRepository
from app.plugins.manager import PluginManager
from app.plugins.snippets import SnippetsPlugin
from app.web import live
from app.web.live import CLOSE, LiveHub, tab_of
from tests.conftest import TEST_USERNAME

OWNER = Identity(WEB, TEST_USERNAME)


class Alone:
    def identities(self, identity):
        return (identity,)


class Paired:
    def identities(self, identity):
        return (Identity(TELEGRAM, "42"), OWNER)


class TestHub:
    def test_a_change_reaches_every_tab_of_the_person(self):
        hub = LiveHub(Paired())
        first, second = hub.subscribe(OWNER), hub.subscribe(OWNER)

        hub.changed(Identity(TELEGRAM, "42"), "/todo", "tab-1")

        assert first.get_nowait() == {"page": "/todo", "origin": "tab-1"}
        assert second.get_nowait() == {"page": "/todo", "origin": "tab-1"}

    def test_a_change_does_not_reach_someone_else(self):
        hub = LiveHub(Alone())
        stranger = hub.subscribe(Identity(WEB, "stranger"))

        hub.changed(OWNER, "/todo")

        assert stranger.empty()

    def test_connections_per_person_are_capped(self, monkeypatch):
        monkeypatch.setattr(live, "MAX_PER_PERSON", 2)
        hub = LiveHub(Alone())

        assert hub.subscribe(OWNER) and hub.subscribe(OWNER)
        assert hub.subscribe(OWNER) is None
        assert hub.subscribe(Identity(WEB, "other")) is not None

    def test_unsubscribing_frees_the_slot(self, monkeypatch):
        monkeypatch.setattr(live, "MAX_PER_PERSON", 1)
        hub = LiveHub(Alone())
        queue = hub.subscribe(OWNER)

        hub.unsubscribe(OWNER, queue)

        assert hub.count() == 0
        assert hub.subscribe(OWNER) is not None

    def test_a_slow_tab_drops_changes_instead_of_growing(self, monkeypatch):
        monkeypatch.setattr(live, "QUEUE_SIZE", 2)
        hub = LiveHub(Alone())
        queue = hub.subscribe(OWNER)

        for _ in range(10):
            hub.changed(OWNER, "/todo")

        assert queue.qsize() == 2

    def test_nothing_subscribes_after_close(self):
        hub = LiveHub(Alone())
        hub.close()

        assert hub.subscribe(OWNER) is None

    def test_an_admin_change_reaches_only_admin_tabs(self):
        hub = LiveHub(Alone())
        admin, plain = hub.subscribe(OWNER, admin=True), hub.subscribe(OWNER)

        hub.admin_changed("/admin/visits")

        assert admin.get_nowait() == {"page": "/admin/visits", "origin": ""}
        assert plain.empty()

    def test_an_unsubscribed_admin_tab_hears_nothing(self):
        hub = LiveHub(Alone())
        admin = hub.subscribe(OWNER, admin=True)

        hub.unsubscribe(OWNER, admin)
        hub.admin_changed("/admin/visits")

        assert admin.empty()

    def test_close_ends_every_stream(self):
        hub = LiveHub(Alone())
        queue = hub.subscribe(OWNER)
        hub.changed(OWNER, "/todo")

        hub.close()

        assert queue.get_nowait() is CLOSE

    @pytest.mark.parametrize(
        "value, expected",
        [("abc-123", "abc-123"), ("", ""), ("a b", ""), ("x" * 65, ""), ("<s>", "")],
    )
    def test_tab_ids_are_strict(self, value, expected):
        request = SimpleNamespace(headers={"X-Tab": value})

        assert tab_of(request) == expected


class TestPluginsReportChanges:
    async def test_a_command_reports_its_page(self, database_with_schema):
        manager = PluginManager(logging.getLogger("test"))
        plugin = SnippetsPlugin(database_with_schema)
        manager.register(plugin)
        seen = []
        plugin.notify_change = lambda *args: seen.append(args)

        await manager.execute("/snippets", "", OWNER)

        assert seen == [(OWNER, "/snippets", "")]


async def _next_event(response, timeout=3):
    async def read():
        lines = []
        while True:
            line = (await response.content.readline()).decode()
            if line.startswith(("event:", "data:")):
                lines.append(line.strip())
            elif line == "\n" and lines:
                if lines[0] == "event: change":
                    return lines
                lines = []

    lines = await asyncio.wait_for(read(), timeout)
    return json.loads(lines[-1].removeprefix("data: "))


class TestTheStream:
    @pytest.fixture
    def server(self, web_server_factory, database_with_schema):
        manager = PluginManager(logging.getLogger("test"))
        manager.register(SnippetsPlugin(database_with_schema))
        return web_server_factory(manager, database_with_schema)

    @pytest.fixture
    async def client(self, server, create_user, login):
        async with TestClient(TestServer(server.build_app())) as test_client:
            await login(test_client)
            yield test_client

    async def _save(self, client, tab="tab-a"):
        return await client.post(
            "/api/page/action",
            json={
                "page": "/snippets",
                "action": "save",
                "payload": {"title": "t", "body": "hello"},
            },
            headers={"X-Tab": tab},
        )

    async def test_a_stranger_gets_no_stream(self, server):
        async with TestClient(TestServer(server.build_app())) as stranger:
            response = await stranger.get("/api/events")

        assert response.status == 401

    async def test_another_tab_hears_about_a_change(self, client):
        events = await client.get("/api/events")
        assert events.headers["Content-Type"].startswith("text/event-stream")
        assert "no-transform" in events.headers["Cache-Control"]
        assert await events.content.readline() == b"retry: 5000\n"
        assert await events.content.readline() == b"event: ping\n"

        assert (await self._save(client)).status == 200

        assert await _next_event(events) == {"page": "/snippets", "origin": "tab-a"}
        events.close()

    async def test_a_view_refresh_is_not_a_change(self, client):
        events = await client.get("/api/events")

        await client.post(
            "/api/page/action",
            json={"page": "/snippets", "action": "view", "payload": {}},
        )
        await self._save(client, "tab-b")

        assert (await _next_event(events))["origin"] == "tab-b"
        events.close()

    async def test_a_refused_action_is_not_a_change(self, client):
        events = await client.get("/api/events")

        refused = await client.post(
            "/api/page/action",
            json={"page": "/snippets", "action": "nonsense", "payload": {}},
        )
        await self._save(client, "tab-c")

        assert refused.status == 400
        assert (await _next_event(events))["origin"] == "tab-c"
        events.close()

    async def test_the_stream_ends_when_the_session_is_revoked(
        self, client, server, database_with_schema, users
    ):
        events = await client.get("/api/events")
        user = users.get_by_username(TEST_USERNAME)
        SessionsRepository(database_with_schema).delete_for_user(user.id)

        server._live.changed(OWNER, "/snippets")

        rest = await asyncio.wait_for(events.content.read(), 3)

        assert b"event: change" not in rest

    async def test_a_linked_chat_change_reaches_the_browser(
        self, client, server, database_with_schema, users
    ):
        user = users.get_by_username(TEST_USERNAME)
        TelegramLinksRepository(database_with_schema).link(user.id, 42)
        events = await client.get("/api/events")

        server._live.changed(Identity(TELEGRAM, "42"), "/snippets")

        assert await _next_event(events) == {"page": "/snippets", "origin": ""}
        events.close()

    async def test_an_admin_hears_about_a_visit(self, client, users):
        users.set_admin(users.get_by_username(TEST_USERNAME).id, True)
        events = await client.get("/api/events")

        await client.get("/about")

        assert await _next_event(events) == {"page": "/admin/visits", "origin": ""}
        assert await _next_event(events) == {"page": "/admin/sessions", "origin": ""}
        events.close()

    async def test_someone_else_does_not_hear_about_visits(self, client):
        events = await client.get("/api/events")

        await client.get("/about")
        await self._save(client)

        assert (await _next_event(events))["page"] == "/snippets"
        events.close()

    async def test_an_admin_hears_about_feedback(self, client, users):
        users.set_admin(users.get_by_username(TEST_USERNAME).id, True)
        events = await client.get("/api/events")

        await client.post("/feedback", json={"text": "Хороший сайт", "page": "/about"})

        assert await _next_event(events) == {"page": "/admin/feedback", "origin": ""}
        events.close()

    async def test_the_console_hears_about_quiet_hours(self, client):
        page = await (await client.get("/")).text()
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        events = await client.get("/api/events")

        await client.post(
            "/account/quiet",
            data={"csrf": csrf, "from": "23:00", "to": "08:00"},
            allow_redirects=False,
        )

        assert await _next_event(events) == {"page": "/", "origin": ""}
        events.close()

    async def test_the_console_marks_its_live_blocks(self, client):
        page = await (await client.get("/")).text()

        assert 'data-live="quiet"' in page
        assert '<script src="/live-page.js" defer>' in page
        assert (await client.get("/live-page.js")).status == 200
