import base64
import hashlib
import html
import json
import os
import re

import cbor2
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import Config
from app.db.passkeys_repo import PasskeysRepository
from app.db.sessions_repo import SessionsRepository
from app.db.settings_repo import MAINTENANCE, SettingsRepository
from app.manage import main
from app.web.auth import AUTH_HEADER, SESSION_COOKIE
from tests.conftest import TEST_USERNAME

ORIGIN = "http://localhost:8080"
RP_ID = "localhost"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Authenticator:
    def __init__(self, origin: str = ORIGIN):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.count = 0
        self.origin = origin

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": self.origin}
        ).encode()

    def _public_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def create(self, options: dict) -> dict:
        client = self._client_data("webauthn.create", options["challenge"])
        data = (
            hashlib.sha256(RP_ID.encode()).digest()
            + bytes([0x45])
            + (0).to_bytes(4, "big")
            + bytes(16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._public_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": data})
        return {
            "id": _b64(self.credential_id),
            "rawId": _b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(client),
                "attestationObject": _b64(attestation),
            },
        }

    def get(self, options: dict) -> dict:
        self.count += 1
        client = self._client_data("webauthn.get", options["challenge"])
        data = (
            hashlib.sha256(RP_ID.encode()).digest()
            + bytes([0x05])
            + self.count.to_bytes(4, "big")
        )
        signature = self.key.sign(
            data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": _b64(self.credential_id),
            "rawId": _b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(client),
                "authenticatorData": _b64(data),
                "signature": _b64(signature),
            },
        }


HEADERS = {AUTH_HEADER: Config.WEB_ACCESS_TOKEN} if Config.WEB_ACCESS_TOKEN else {}


@pytest.fixture(autouse=True)
def origin(monkeypatch):
    monkeypatch.setattr("app.web.passkeys.ORIGIN", ORIGIN)


@pytest.fixture
async def new_client(manager, database_with_schema, web_server_factory):
    server = web_server_factory(manager, database_with_schema)
    made = []

    async def _make() -> TestClient:
        client = TestClient(TestServer(server.build_app()))
        await client.start_server()
        made.append(client)
        return client

    yield _make

    for client in made:
        await client.close()


@pytest.fixture
def admin(create_user, users):
    account = users.get_by_username(TEST_USERNAME)
    users.set_admin(account.id, True)
    return account


async def _csrf(client) -> str:
    body = await (await client.get("/", headers=HEADERS)).text()
    return re.search(r'id="passkeys" data-csrf="([^"]+)"', body).group(1)


async def _enrol(client, authenticator: Authenticator):
    csrf = await _csrf(client)
    options = await client.post(
        "/api/passkeys/options", json={"csrf": csrf}, headers=HEADERS
    )
    assert options.status == 200, await options.text()
    credential = authenticator.create(await options.json())
    return await client.post(
        "/api/passkeys", json={"csrf": csrf, "credential": credential}, headers=HEADERS
    )


def _options(body: str) -> dict:
    found = re.search(r'data-options="([^"]+)"', body).group(1)
    return json.loads(html.unescape(found))


def _session(client) -> str:
    return next(
        (c.value for c in client.session.cookie_jar if c.key == SESSION_COOKIE), ""
    )


class TestTheAdministrator:
    async def test_a_password_alone_does_not_open_the_admin_area(
        self, new_client, login, admin
    ):
        client = await new_client()
        await login(client)

        assert (await client.get("/admin", headers=HEADERS)).status == 404
        body = await (await client.get("/", headers=HEADERS)).text()
        assert "Админка открывается только после входа с passkey" in body
        assert 'href="/admin"' not in body

    async def test_the_first_passkey_opens_it(self, new_client, login, admin):
        client = await new_client()
        await login(client)

        response = await _enrol(client, Authenticator())

        assert response.status == 200, await response.text()
        assert (await client.get("/admin", headers=HEADERS)).status == 200

    async def test_after_that_the_password_asks_for_the_passkey(
        self, new_client, login, admin
    ):
        authenticator = Authenticator()
        first = await new_client()
        await login(first)
        await _enrol(first, authenticator)

        client = await new_client()
        step = await login(client, expect_status=200)

        assert _session(client) == ""
        options = _options(await step.text())
        finish = await client.post(
            "/login/passkey",
            json={"credential": authenticator.get(options)},
            headers=HEADERS,
        )

        assert finish.status == 200, await finish.text()
        assert (await finish.json())["location"] == "/"
        assert (await client.get("/admin", headers=HEADERS)).status == 200


    async def test_maintenance_does_not_lock_the_passkey_out(
        self, new_client, login, admin, users, monkeypatch
    ):
        monkeypatch.setattr("app.db.settings_repo.CACHE_SECONDS", 0)
        authenticator = Authenticator()
        first = await new_client()
        await login(first)
        await _enrol(first, authenticator)
        SettingsRepository(users.database).put(MAINTENANCE, "1")

        client = await new_client()
        step = await login(client, expect_status=200)
        script = await client.get("/passkey.js", headers=HEADERS)
        finish = await client.post(
            "/login/passkey",
            json={"credential": authenticator.get(_options(await step.text()))},
            headers=HEADERS,
        )

        assert script.status == 200
        assert finish.status == 200, await finish.text()
        assert (await client.get("/admin", headers=HEADERS)).status == 200


class TestRefusals:
    @pytest.fixture
    async def enrolled(self, new_client, login, admin):
        authenticator = Authenticator()
        client = await new_client()
        await login(client)
        await _enrol(client, authenticator)
        return authenticator

    async def test_a_phishing_origin_is_refused_and_the_step_is_spent(
        self, new_client, login, enrolled
    ):
        client = await new_client()
        options = _options(await (await login(client, expect_status=200)).text())
        enrolled.origin = "https://example.com"

        refused = await client.post(
            "/login/passkey",
            json={"credential": enrolled.get(options)},
            headers=HEADERS,
        )
        enrolled.origin = ORIGIN
        again = await client.post(
            "/login/passkey",
            json={"credential": enrolled.get(options)},
            headers=HEADERS,
        )

        assert refused.status == 401
        assert again.status == 400
        assert _session(client) == ""

    async def test_another_key_cannot_answer(self, new_client, login, enrolled):
        client = await new_client()
        options = _options(await (await login(client, expect_status=200)).text())

        refused = await client.post(
            "/login/passkey",
            json={"credential": Authenticator().get(options)},
            headers=HEADERS,
        )

        assert refused.status == 401
        assert _session(client) == ""

    async def test_an_old_password_session_cannot_add_a_key(
        self, new_client, login, admin, users
    ):
        client = await new_client()
        await login(client)
        csrf = await _csrf(client)
        await _enrol(await self._second(new_client, login), Authenticator())

        refused = await client.post(
            "/api/passkeys/options", json={"csrf": csrf}, headers=HEADERS
        )

        assert refused.status == 403
        assert len(PasskeysRepository(users.database).for_user(admin.id)) == 1

    async def test_an_old_password_session_cannot_remove_a_key(
        self, new_client, login, admin, users
    ):
        client = await new_client()
        await login(client)
        csrf = await _csrf(client)
        await _enrol(await self._second(new_client, login), Authenticator())
        key = PasskeysRepository(users.database).for_user(admin.id)[0]

        await client.post(
            "/account/passkeys/remove",
            data={"csrf": csrf, "id": str(key.id)},
            headers=HEADERS,
            allow_redirects=False,
        )

        assert len(PasskeysRepository(users.database).for_user(admin.id)) == 1

    async def test_a_forged_request_adds_nothing(self, new_client, login, admin):
        client = await new_client()
        await login(client)

        refused = await client.post(
            "/api/passkeys/options", json={"csrf": "forged"}, headers=HEADERS
        )

        assert refused.status == 403

    @staticmethod
    async def _second(new_client, login):
        client = await new_client()
        await login(client)
        return client


class TestReset:
    def test_it_removes_the_keys_and_the_sessions(self, admin, users):
        database = users.database
        PasskeysRepository(database).add(admin.id, b"id", b"key", 0, "Mac")
        token = SessionsRepository(database).create(admin.id, 3600, passkey=True)

        assert main(["reset-passkeys", TEST_USERNAME]) == 0

        assert PasskeysRepository(database).for_user(admin.id) == []
        assert SessionsRepository(database).get_user_id(token) is None
