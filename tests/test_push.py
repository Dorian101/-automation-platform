import base64
import json
import logging
import re
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pywebpush import WebPushException

from app.core.config import Config
from app.core.identity import TELEGRAM, WEB, Identity
from app.db.links_repo import TelegramLinksRepository
from app.db.push_repo import (
    MAX_DEVICES,
    BadSubscription,
    PushRepository,
    TooManyDevices,
    checked,
)
from app.notifications import (
    LinkedChatResolver,
    Notifier,
    TelegramChannel,
    WebPushChannel,
)
from app.plugins.manager import PluginManager
from tests.conftest import TEST_USERNAME

OWNER = Identity(kind=WEB, id=TEST_USERNAME)
APPLE = "https://web.push.apple.com/"


def _b64(size: int) -> str:
    return base64.urlsafe_b64encode(b"k" * size).rstrip(b"=").decode()


def _point() -> str:
    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    raw = key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


POINT = _point()


def _subscription(endpoint=APPLE + "abc", p256dh=None, auth=None):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh or POINT, "auth": auth or _b64(16)},
    }


@pytest.fixture
def push(database_with_schema):
    return PushRepository(database_with_schema)


@pytest.fixture
def owner_id(create_user, users):
    return users.get_by_username(TEST_USERNAME).id


class Sender:
    def __init__(self, status=None):
        self.calls = []
        self.status = status

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.status:
            raise WebPushException(
                "x", response=SimpleNamespace(status_code=self.status)
            )


class TestSubscriptionCheck:
    def test_a_known_push_service_is_accepted(self):
        assert checked(_subscription())[0] == APPLE + "abc"

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http" + "://web.push.apple.com/abc",
            "https://" + "push.apple.com.evil.test/abc",
            "https://" + "evil.test/web.push.apple.com",
            "https://web.push.apple.com:8443/abc",
            "https://" + "user@web.push.apple.com/abc",
            "https://" + "127.0.0.1/abc",
            "https://" + "evil.test\\.fcm.googleapis.com/x",
            "https://" + "127.0.0.1\\.fcm.googleapis.com/x",
            "https://web.push.apple.com:abc/x",
            "https://" + "\uff03@web.push.apple.com/x",
            "https://" + "WEB.push.apple.com/x",
            "https://" + "web.push.apple.com./x",
            "",
            5,
        ],
    )
    def test_any_other_address_is_refused(self, endpoint):
        with pytest.raises(BadSubscription):
            checked(_subscription(endpoint=endpoint))

    @pytest.mark.parametrize(
        "keys",
        [
            {"p256dh": _b64(64)},
            {"p256dh": _b64(65)},
            {"auth": _b64(15)},
            {"auth": "!!"},
        ],
    )
    def test_bad_keys_are_refused(self, keys):
        with pytest.raises(BadSubscription):
            checked(_subscription(**keys))


class TestRepository:
    def test_a_device_is_stored_once_and_moves_with_its_owner(
        self, push, owner_id, users
    ):
        other = users.create("other", "another-long-password").id
        assert push.add(owner_id, _subscription(), "Mac · Safari") is True
        assert push.add(owner_id, _subscription(), "Mac · Safari") is False

        assert len(push.devices(owner_id)) == 1

        push.add(other, _subscription(), "Mac · Safari")

        assert push.devices(owner_id) == []
        assert len(push.devices(other)) == 1

    def test_devices_are_capped(self, push, owner_id):
        for number in range(MAX_DEVICES):
            push.add(owner_id, _subscription(endpoint=f"{APPLE}{number}"), "x")

        with pytest.raises(TooManyDevices):
            push.add(owner_id, _subscription(endpoint=APPLE + "more"), "x")

    def test_removing_is_scoped_to_the_owner(self, push, owner_id, users):
        other = users.create("other", "another-long-password").id
        push.add(owner_id, _subscription(), "x")
        device = push.devices(owner_id)[0]["id"]

        assert push.remove(other, device) is False
        assert push.remove(owner_id, device) is True

    def test_a_disabled_account_gets_nothing(self, push, owner_id, users):
        push.add(owner_id, _subscription(), "x")
        users.set_active(owner_id, False)

        assert push.subscriptions(OWNER) == []


class TestSessions:
    def test_signing_out_drops_only_that_sessions_device(self, push, owner_id):
        push.add(owner_id, _subscription(endpoint=APPLE + "1"), "x", "session-a")
        push.add(owner_id, _subscription(endpoint=APPLE + "2"), "x", "session-b")

        push.forget_session("session-a")

        assert [d["label"] for d in push.devices(owner_id)] == ["x"]
        assert push.subscriptions(OWNER)[0]["endpoint"] == APPLE + "2"

    def test_revoking_sessions_or_a_new_password_drops_every_device(
        self, push, owner_id, users, database_with_schema
    ):
        from app.db.sessions_repo import SessionsRepository

        push.add(owner_id, _subscription(endpoint=APPLE + "1"), "x")
        SessionsRepository(database_with_schema).delete_for_user(owner_id)
        assert push.devices(owner_id) == []

        push.add(owner_id, _subscription(endpoint=APPLE + "2"), "x")
        users.set_password(owner_id, "another-long-password")
        assert push.devices(owner_id) == []


class TestChannel:
    def test_sends_go_out_without_redirects_or_proxies(self):
        from app.notifications import _session

        session = _session()

        assert (session.max_redirects, session.trust_env) == (0, False)

    async def test_every_device_gets_the_message(self, push, owner_id):
        push.add(owner_id, _subscription(endpoint=APPLE + "1"), "x")
        push.add(owner_id, _subscription(endpoint=APPLE + "2"), "x")
        sender = Sender()
        channel = WebPushChannel(push, "key", "mailto:a@example.com", send=sender)

        assert await channel.deliver(OWNER, "Позвонить") is True
        assert len(sender.calls) == 2
        assert sender.calls[0]["requests_session"].max_redirects == 0
        body = json.loads(sender.calls[0]["data"])
        assert body["body"] == "Позвонить"
        assert body["url"] == "/"

    async def test_a_gone_device_is_forgotten(self, push, owner_id):
        push.add(owner_id, _subscription(), "x")
        channel = WebPushChannel(push, "key", "mailto:a@example.com", send=Sender(410))

        assert await channel.deliver(OWNER, "x") is False
        assert push.devices(owner_id) == []

    async def test_the_console_hears_that_a_device_is_gone(self, push, owner_id):
        push.add(owner_id, _subscription(), "x")
        channel = WebPushChannel(push, "key", "mailto:a@example.com", send=Sender(410))
        heard = []
        channel.on_forgotten = heard.append

        await channel.deliver(OWNER, "x")

        assert heard == [OWNER]

    async def test_a_failed_send_keeps_the_device(self, push, owner_id):
        push.add(owner_id, _subscription(), "x")
        channel = WebPushChannel(push, "key", "mailto:a@example.com", send=Sender(500))

        assert await channel.deliver(OWNER, "x") is False
        assert len(push.devices(owner_id)) == 1

    async def test_without_keys_nothing_is_sent(self, push, owner_id):
        push.add(owner_id, _subscription(), "x")
        sender = Sender()
        channel = WebPushChannel(push, "", "", send=sender)

        assert await channel.deliver(OWNER, "x") is False
        assert channel.reaches(OWNER) is False
        assert sender.calls == []


class TestRouting:
    @pytest.fixture
    def resolver(self, database_with_schema, users, push):
        links = TelegramLinksRepository(database_with_schema)
        return links, LinkedChatResolver(users, links, push)

    def test_devices_come_before_telegram(self, resolver, push, owner_id):
        links, resolve = resolver
        links.link(owner_id, 4242)

        assert resolve(OWNER).kind == TELEGRAM

        push.add(owner_id, _subscription(), "x")

        assert resolve(OWNER) == OWNER
        assert resolve(Identity(kind=TELEGRAM, id="4242")) == OWNER

    def test_an_unlinked_chat_stays_a_chat(self, resolver):
        _, resolve = resolver
        chat = Identity(kind=TELEGRAM, id="77")

        assert resolve(chat) == chat

    def test_reachable_follows_the_devices(self, resolver, push, owner_id):
        _, resolve = resolver
        channel = WebPushChannel(push, "key", "mailto:a@example.com", send=Sender())
        notifier = Notifier([TelegramChannel(bot=None), channel], resolver=resolve)

        assert notifier.reachable(OWNER) is False

        push.add(owner_id, _subscription(), "x")

        assert notifier.reachable(OWNER) is True


class TestRoutes:
    @pytest.fixture
    def configured(self, monkeypatch):
        monkeypatch.setattr(Config, "VAPID_PUBLIC_KEY", _b64(65))
        monkeypatch.setattr(Config, "VAPID_PRIVATE_KEY", "private")
        monkeypatch.setattr(Config, "VAPID_SUBJECT", "mailto:a@example.com")

    @pytest.fixture
    def sender(self):
        return Sender()

    @pytest.fixture
    def web_server(self, database_with_schema, web_server_factory, users, push, sender):
        links = TelegramLinksRepository(database_with_schema)
        notifier = Notifier(
            [WebPushChannel(push, "private", "mailto:a@example.com", send=sender)],
            resolver=LinkedChatResolver(users, links, push),
        )
        return web_server_factory(
            PluginManager(logging.getLogger("test")),
            database_with_schema,
            notifier=notifier,
        )

    @pytest.fixture
    async def client(self, web_server, create_user, login):
        async with TestClient(TestServer(web_server.build_app())) as test_client:
            await login(test_client)
            yield test_client

    async def _csrf(self, client):
        body = await (await client.get("/")).text()
        return re.search(r'id="push" data-csrf="([^"]+)"', body).group(1)

    async def test_the_panel_needs_server_keys(self, client):
        body = await (await client.get("/")).text()

        assert "не настроены" in body
        assert 'id="push"' not in body

    async def test_subscribing_needs_the_form_token(
        self, client, configured, push, owner_id
    ):
        forged = await client.post(
            "/api/push/subscribe", json={"subscription": _subscription(), "csrf": "x"}
        )
        token = await self._csrf(client)
        bad = await client.post(
            "/api/push/subscribe",
            json={
                "subscription": _subscription(endpoint="https://" + "evil.test/x"),
                "csrf": token,
            },
        )
        good = await client.post(
            "/api/push/subscribe", json={"subscription": _subscription(), "csrf": token}
        )

        assert (forged.status, bad.status, good.status) == (403, 400, 200)
        assert len(push.devices(owner_id)) == 1

    async def test_signing_out_drops_this_browsers_device(
        self, client, configured, push, owner_id
    ):
        token = await self._csrf(client)
        await client.post(
            "/api/push/subscribe", json={"subscription": _subscription(), "csrf": token}
        )
        assert len(push.devices(owner_id)) == 1

        await client.post("/logout", data={"csrf": token})

        assert push.devices(owner_id) == []

    async def test_a_stranger_cannot_subscribe(self, web_server, configured):
        async with TestClient(TestServer(web_server.build_app())) as stranger:
            response = await stranger.post(
                "/api/push/subscribe", json={"subscription": _subscription()}
            )

        assert response.status == 401

    async def test_the_test_button_reaches_the_devices(
        self, client, configured, push, owner_id, sender
    ):
        push.add(owner_id, _subscription(), "x")
        token = await self._csrf(client)

        response = await client.post("/api/push/test", json={"csrf": token})

        assert response.status == 200
        assert len(sender.calls) == 1

    async def test_a_device_is_removed_from_the_page(
        self, client, configured, push, owner_id
    ):
        push.add(owner_id, _subscription(), "x")
        token = await self._csrf(client)
        device = push.devices(owner_id)[0]["id"]

        await client.post(
            "/account/push/remove", data={"csrf": token, "id": str(device)}
        )

        assert push.devices(owner_id) == []

    async def test_the_worker_is_public_script(self, web_server):
        async with TestClient(TestServer(web_server.build_app())) as stranger:
            response = await stranger.get("/sw.js")
            text = await response.text()

        assert response.status == 200
        assert response.content_type == "text/javascript"
        assert "showNotification" in text
