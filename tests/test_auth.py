import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.web.auth import AUTH_HEADER, SESSION_COOKIE
from app.web.server import WebServer
from tests.conftest import TEST_USERNAME

TOKEN = "s3cret-token-value"

WRONG_CREDENTIALS_MESSAGE = "Incorrect username or password."


def _proxy_headers() -> dict[str, str]:
    """What the reverse proxy attaches to every request it forwards."""
    return {AUTH_HEADER: Config.WEB_ACCESS_TOKEN} if Config.WEB_ACCESS_TOKEN else {}


def _client(server, proxy: bool = True) -> TestClient:
    headers = _proxy_headers() if proxy else None
    return TestClient(TestServer(server.build_app()), headers=headers)


@pytest.fixture
def secured_server(manager, database_with_schema, monkeypatch):
    monkeypatch.setattr(Config, "WEB_ACCESS_TOKEN", TOKEN)

    return WebServer(manager, database_with_schema, "127.0.0.1", 0)


@pytest.fixture
async def bare_client(secured_server):
    """No proxy header and no session: only for exercising the transport layer."""
    async with _client(secured_server, proxy=False) as test_client:
        yield test_client


@pytest.fixture
async def anonymous_client(secured_server, create_user):
    """A real account and a valid proxy header, but no session yet."""
    async with _client(secured_server) as test_client:
        yield test_client


@pytest.fixture
async def client(secured_server, create_user, login):
    async with _client(secured_server) as test_client:
        await login(test_client)
        yield test_client


class TestTransportLayer:
    """The shared secret proves a request came through the proxy.

    It says nothing about who the user is, so a valid secret on its own must
    still land on the login form.
    """

    async def test_rejects_missing_header(self, bare_client):
        response = await bare_client.get("/")

        assert response.status == 401

    async def test_rejects_wrong_header(self, bare_client):
        response = await bare_client.get(
            "/api/commands",
            headers={AUTH_HEADER: "wrong"},
        )

        assert response.status == 401

    async def test_rejects_empty_header(self, bare_client):
        response = await bare_client.get(
            "/api/commands",
            headers={AUTH_HEADER: ""},
        )

        assert response.status == 401

    async def test_secret_alone_does_not_open_the_app(self, bare_client):
        response = await bare_client.get(
            "/",
            headers={AUTH_HEADER: TOKEN},
            allow_redirects=False,
        )

        assert response.status == 303
        assert response.headers["Location"] == "/login"

    async def test_login_page_requires_the_secret(self, bare_client):
        response = await bare_client.get("/login")

        assert response.status == 401

    async def test_disabled_when_token_unset(
        self,
        manager,
        database_with_schema,
        create_user,
        login,
        monkeypatch,
    ):
        monkeypatch.setattr(Config, "WEB_ACCESS_TOKEN", "")

        server = WebServer(manager, database_with_schema, "127.0.0.1", 0)

        async with _client(server) as test_client:
            await login(test_client)

            response = await test_client.get("/api/commands")

        assert response.status == 200


class TestSessionLayer:
    async def test_index_redirects_to_login_without_session(self, anonymous_client):
        response = await anonymous_client.get("/", allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == "/login"

    async def test_api_returns_401_without_session(self, anonymous_client):
        response = await anonymous_client.post(
            "/api/command",
            json={"command": "/notes", "args": ""},
        )

        assert response.status == 401

    async def test_health_works_without_a_session(self, anonymous_client):
        """A health check must answer even when nobody is signed in."""
        response = await anonymous_client.get("/health")

        assert response.status == 200
        assert (await response.json()) == {"status": "ok", "database": True}

    async def test_login_page_is_reachable(self, anonymous_client):
        response = await anonymous_client.get("/login")

        assert response.status == 200
        assert response.content_type == "text/html"

    async def test_login_page_redirects_when_already_signed_in(self, client):
        response = await client.get("/login", allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == "/"

    async def test_index_shows_the_signed_in_user(self, client):
        response = await client.get("/")

        assert response.status == 200
        assert TEST_USERNAME in await response.text()


class TestLogin:
    async def test_rejects_wrong_password(self, anonymous_client, login):
        response = await login(
            anonymous_client,
            password="not-the-password",
            expect_status=401,
        )

        assert WRONG_CREDENTIALS_MESSAGE in await response.text()

    async def test_rejects_unknown_user_with_the_same_message(
        self,
        anonymous_client,
        login,
    ):
        """An attacker must not be able to learn which names are taken."""
        response = await login(
            anonymous_client,
            username="nobody-here",
            password="whatever-1234",
            expect_status=401,
        )

        assert WRONG_CREDENTIALS_MESSAGE in await response.text()

    async def test_empty_password_is_rejected(self, anonymous_client, login):
        await login(anonymous_client, password="", expect_status=401)

    async def test_success_sets_the_session_cookie(self, anonymous_client, login):
        response = await login(anonymous_client)

        assert response.headers["Location"] == "/"
        assert SESSION_COOKIE in response.cookies

    async def test_success_grants_access(self, anonymous_client, login):
        await login(anonymous_client)

        response = await anonymous_client.get("/")

        assert response.status == 200

    async def test_username_is_matched_case_insensitively(
        self,
        anonymous_client,
        login,
    ):
        response = await login(anonymous_client, username=TEST_USERNAME.upper())

        assert response.headers["Location"] == "/"


class TestSessionCookie:
    async def test_is_marked_httponly_and_same_site(self, anonymous_client, login):
        response = await login(anonymous_client)

        header = response.headers["Set-Cookie"]

        assert SESSION_COOKIE in header
        assert "httponly" in header.lower()
        assert "samesite=lax" in header.lower()

    async def test_is_secure_in_production(self, anonymous_client, login, monkeypatch):
        monkeypatch.setattr(Config, "SESSION_COOKIE_SECURE", True)

        response = await login(anonymous_client)

        assert "secure" in response.headers["Set-Cookie"].lower()

    async def test_is_not_secure_over_plain_http(self, anonymous_client, login):
        response = await login(anonymous_client)

        assert "secure" not in response.headers["Set-Cookie"].lower()


class TestLogout:
    async def test_ends_the_session(self, client):
        response = await client.post("/logout", allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == "/login"

        after = await client.get("/", allow_redirects=False)

        assert after.status == 303
        assert after.headers["Location"] == "/login"

    async def test_survives_a_repeated_logout(self, client):
        await client.post("/logout", allow_redirects=False)

        response = await client.post("/logout", allow_redirects=False)

        assert response.status == 303


class TestDenial:
    async def test_command_is_not_executed_when_denied(
        self,
        anonymous_client,
        database_with_schema,
    ):
        await anonymous_client.post(
            "/api/command",
            json={"command": "/add", "args": "must not persist"},
        )

        from app.core.identity import WEB, Identity
        from app.db.notes_repo import NotesRepository

        repo = NotesRepository(database_with_schema)

        assert repo.list(Identity(kind=WEB, id=TEST_USERNAME)) == []

    async def test_command_runs_once_signed_in(self, client):
        response = await client.post(
            "/api/command",
            json={"command": "/add", "args": "note"},
        )

        assert response.status == 200
        assert (await response.json())["result"] == "Saved"
