import asyncio
import logging
import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.identity import WEB, Identity
from app.db.api_keys_repo import KEY_SHAPE, MAX_KEYS
from app.plugins.bookmarks import BookmarksPlugin
from app.plugins.manager import PluginManager
from tests.conftest import TEST_USERNAME

OWNER = Identity(kind=WEB, id=TEST_USERNAME)


async def _title(url):
    return "Title"


@pytest.fixture
def plugin(database_with_schema):
    return BookmarksPlugin(database_with_schema, fetch=_title)


@pytest.fixture
def web_server(plugin, database_with_schema, web_server_factory):
    manager = PluginManager(logging.getLogger("test"))
    manager.register(plugin)
    return web_server_factory(manager, database_with_schema)


@pytest.fixture
async def client(web_server, create_user, login):
    async with TestClient(TestServer(web_server.build_app())) as test_client:
        await login(test_client)
        yield test_client


@pytest.fixture
async def stranger(web_server):
    async with TestClient(TestServer(web_server.build_app())) as test_client:
        yield test_client


async def _settle(plugin):
    await asyncio.gather(*list(plugin._pending))


async def _new_key(plugin, name="iPhone"):
    view = await plugin.page_action(OWNER, "keycreate", {"name": name})
    assert view["hold"] is True
    return KEY_SHAPE.search(view["notice"]).group(0)


def _send(client, key, body, **headers):
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    return client.post("/api/links", json=body, headers=headers)


class TestKeys:
    async def test_a_key_is_shown_once_and_only_its_hash_is_kept(
        self, plugin, create_user, database_with_schema
    ):
        key = await _new_key(plugin)

        with database_with_schema.connect() as conn:
            stored = conn.execute("SELECT key_hash FROM api_keys").fetchone()[0]
        view = await plugin.page_view(OWNER)

        assert key not in stored
        assert key not in str(view)
        assert "hold" not in view
        assert view["list"][1]["rows"][0]["name"] == "iPhone"

    async def test_keys_are_capped(self, plugin, create_user):
        for _ in range(MAX_KEYS):
            await _new_key(plugin)

        with pytest.raises(Exception, match="не больше"):
            await _new_key(plugin)

    async def test_someone_else_cannot_revoke_a_key(self, plugin, create_user, users):
        users.create("other", "another-long-password")
        await _new_key(plugin)
        key_id = plugin.keys.entries(OWNER)[0]["id"]

        with pytest.raises(Exception, match="не найден"):
            await plugin.page_action(
                Identity(kind=WEB, id="other"), "keyrevoke", {"id": str(key_id)}
            )

        assert len(plugin.keys.entries(OWNER)) == 1


class TestApi:
    async def test_a_key_saves_a_link_for_its_owner(
        self, stranger, plugin, create_user
    ):
        key = await _new_key(plugin)

        response = await _send(
            stranger, key, {"url": "https://example.com/a", "text": "почитать"}
        )

        assert response.status == 200
        assert (await response.json())["text"] == "Сохранено: example.com"
        [entry] = plugin.repo.entries(OWNER)
        assert (entry["url"], entry["description"]) == (
            "https://example.com/a",
            "почитать",
        )
        await _settle(plugin)
        assert plugin.keys.entries(OWNER)[0]["last_used_at"] is not None

    @pytest.mark.parametrize(
        "key",
        [None, "", "ap_" + "x" * 43, "not-a-key", "ap_" + "x" * 500],
    )
    async def test_a_missing_or_wrong_key_is_refused(
        self, stranger, plugin, create_user, key
    ):
        response = await _send(stranger, key, {"url": "https://example.com"})

        assert response.status == 401
        assert plugin.repo.entries(OWNER) == []

    async def test_a_session_cookie_alone_is_not_enough(self, client, plugin):
        response = await _send(client, None, {"url": "https://example.com"})

        assert response.status == 401
        assert plugin.repo.entries(OWNER) == []

    async def test_a_revoked_key_stops_working(self, stranger, plugin, create_user):
        key = await _new_key(plugin)
        key_id = plugin.keys.entries(OWNER)[0]["id"]
        await plugin.page_action(OWNER, "keyrevoke", {"id": str(key_id)})

        response = await _send(stranger, key, {"url": "https://example.com"})

        assert response.status == 401

    async def test_a_disabled_account_stops_its_keys(
        self, stranger, plugin, create_user, users
    ):
        key = await _new_key(plugin)
        users.set_active(users.get_by_username(TEST_USERNAME).id, False)

        response = await _send(stranger, key, {"url": "https://example.com"})

        assert response.status == 401

    async def test_a_new_password_revokes_the_keys(
        self, stranger, plugin, create_user, users
    ):
        key = await _new_key(plugin)
        users.set_password(users.get_by_username(TEST_USERNAME).id, "another-long-one")

        response = await _send(stranger, key, {"url": "https://example.com"})

        assert response.status == 401
        assert plugin.keys.entries(OWNER) == []

    async def test_a_deleted_account_takes_its_keys_with_it(
        self, plugin, create_user, database_with_schema
    ):
        await _new_key(plugin)

        with database_with_schema.connect() as conn:
            conn.execute("DELETE FROM users")
            left = conn.execute("SELECT count(*) FROM api_keys").fetchone()[0]

        assert left == 0

    async def test_guessing_keys_is_throttled(self, stranger, create_user):
        statuses = [
            (await _send(stranger, "ap_" + "y" * 43, {"url": "x"})).status
            for _ in range(12)
        ]

        assert statuses[:10] == [401] * 10
        assert statuses[-1] == 429

    @pytest.mark.parametrize(
        "body", [{"url": ""}, {"url": 5}, {"text": "x"}, {"url": "a", "text": 1}]
    )
    async def test_a_bad_body_is_refused(self, stranger, plugin, create_user, body):
        key = await _new_key(plugin)

        response = await _send(stranger, key, body)

        assert response.status == 400

    async def test_a_large_body_is_refused(self, stranger, plugin, create_user):
        key = await _new_key(plugin)

        response = await _send(
            stranger, key, {"url": "https://example.com", "text": "x" * 9000}
        )

        assert response.status == 413


class TestBookmarklet:
    async def test_the_page_offers_the_button(self, client):
        body = await (await client.get("/links/save")).text()

        assert 'class="bookmarklet" href="javascript:' in body
        assert "/links/save?url=" in body

    async def test_opening_it_saves_nothing(self, client, plugin):
        body = await (await client.get("/links/save?url=https://example.com/x")).text()

        assert "https://example.com/x" in body
        assert plugin.repo.entries(OWNER) == []

    async def test_the_url_is_escaped(self, client):
        body = await (
            await client.get('/links/save?url=https://example.com/"><script>x</script>')
        ).text()

        assert "<script>x</script>" not in body

    async def test_saving_takes_the_form_token(self, client, plugin):
        body = await (await client.get("/links/save?url=https://example.com/x")).text()
        token = re.search(r'name="csrf" value="([^"]+)"', body).group(1)

        forged = await client.post(
            "/links/save", data={"csrf": "nope", "url": "https://example.com/y"}
        )
        saved = await client.post(
            "/links/save",
            data={"csrf": token, "url": "https://example.com/x", "text": "в обед"},
        )

        assert forged.status == 403
        assert "Сохранено: example.com" in await saved.text()
        [entry] = plugin.repo.entries(OWNER)
        assert (entry["url"], entry["description"]) == (
            "https://example.com/x",
            "в обед",
        )

    async def test_a_stranger_is_sent_to_sign_in(self, stranger):
        response = await stranger.get(
            "/links/save?url=https://example.com", allow_redirects=False
        )

        assert response.status in (302, 303)
        assert response.headers["Location"].startswith("/login")

    async def test_the_window_cannot_be_framed(self, client):
        response = await client.get("/links/save?url=https://example.com")

        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
