import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.web.auth import AUTH_HEADER, SESSION_COOKIE, signup_allowed
from app.web.server import WebServer
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def _texts(entries):
    """The note text alone, so assertions do not repeat the row id."""
    return [text for _, text in entries]

TOKEN = "s3cret-token-value"

INVITE_CODE = "correct-horse-invite"

WRONG_CREDENTIALS_MESSAGE = "Incorrect username or password."


def _proxy_headers() -> dict[str, str]:
    """What the reverse proxy attaches to every request it forwards."""
    return {AUTH_HEADER: Config.WEB_ACCESS_TOKEN} if Config.WEB_ACCESS_TOKEN else {}


def _client(server, proxy: bool = True) -> TestClient:
    headers = _proxy_headers() if proxy else None
    return TestClient(TestServer(server.build_app()), headers=headers)


async def _signup(
    client,
    *,
    invite: str = INVITE_CODE,
    username: str = "newcomer",
    password: str = TEST_PASSWORD,
):
    """Submit the registration form the way a browser behind the proxy would."""
    return await client.post(
        "/signup",
        data={"invite": invite, "username": username, "password": password},
        allow_redirects=False,
    )


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

        assert _texts(repo.entries(Identity(kind=WEB, id=TEST_USERNAME))) == []

    async def test_command_runs_once_signed_in(self, client):
        response = await client.post(
            "/api/command",
            json={"command": "/add", "args": "note"},
        )

        assert response.status == 200
        assert (await response.json())["result"] == "Saved"


@pytest.fixture
def with_signup(monkeypatch):
    monkeypatch.setattr(Config, "SIGNUP_INVITE_CODE", INVITE_CODE)


@pytest.fixture
async def guest_client(secured_server):
    """A valid proxy header and no session, and nobody created for me.

    Unlike anonymous_client this does not create an account, so a test can
    ask whether a form produced a user without a pre-existing one muddling
    the count.
    """
    async with _client(secured_server) as test_client:
        yield test_client


@pytest.fixture
async def signup_client(guest_client, with_signup):
    """A guest with sign-up switched on."""
    yield guest_client


class TestSignup:
    """The invite code is the whole door.

    There is no approval step behind it — whoever holds the code is in — so
    what is worth testing is that the code is checked, that a failed attempt
    leaves nothing behind, and that a successful one leaves the user signed
    in rather than back at the login form.
    """

    async def test_page_is_reachable_without_a_session(self, signup_client):
        response = await signup_client.get("/signup")

        assert response.status == 200
        assert "Invite code" in await response.text()

    async def test_still_needs_the_proxy_secret(self, with_signup, bare_client):
        response = await bare_client.get("/signup")

        assert response.status == 401

    async def test_wrong_invite_code_creates_nothing(
        self,
        signup_client,
        users,
    ):
        response = await _signup(signup_client, invite="not-the-code")

        assert response.status == 403
        assert "Invalid invite code." in await response.text()
        assert users.count() == 0

    async def test_valid_invite_code_creates_the_account_and_signs_in(
        self,
        signup_client,
        users,
    ):
        response = await _signup(signup_client)

        assert response.status == 303
        assert response.headers["Location"] == "/"
        assert SESSION_COOKIE in response.cookies
        assert users.count() == 1

    async def test_pasted_invite_code_with_stray_whitespace_still_works(
        self,
        signup_client,
        users,
    ):
        """A code copied out of a terminal or a chat often drags a newline."""
        response = await _signup(signup_client, invite=f"  {INVITE_CODE}\n")

        assert response.status == 303
        assert users.count() == 1

    async def test_new_account_can_use_the_platform(self, signup_client):
        await _signup(signup_client)

        response = await signup_client.get("/")

        assert response.status == 200

    async def test_short_password_is_reported(self, signup_client, users):
        response = await _signup(signup_client, password="short")

        assert response.status == 400
        assert "at least 8 characters" in await response.text()
        assert users.count() == 0

    async def test_blank_username_is_reported(self, signup_client, users):
        response = await _signup(signup_client, username="   ")

        assert response.status == 400
        assert "must not be empty" in await response.text()
        assert users.count() == 0

    async def test_taken_username_is_reported(self, signup_client, users):
        users.create(TEST_USERNAME, TEST_PASSWORD)

        response = await _signup(signup_client, username=TEST_USERNAME.upper())

        assert response.status == 409
        assert "taken" in await response.text()
        assert users.count() == 1

    async def test_login_page_offers_the_link(self, guest_client, with_signup):
        response = await guest_client.get("/login")

        assert 'href="/signup"' in await response.text()

    async def test_every_placeholder_is_replaced(self, guest_client, with_signup):
        """A placeholder nobody substituted shows up as literal text on the page.

        Both pages and both branches of the form are rendered, because a
        missing replacement in the error path would otherwise go unnoticed
        until somebody saw `__ERROR_BLOCK__` in a browser.
        """
        tokens = (
            "__STYLES__",
            "__ERROR_BLOCK__",
            "__USERNAME__",
            "__SIGNUP_BLOCK__",
        )

        pages = [
            await guest_client.get("/login"),
            await guest_client.get("/signup"),
            await _signup(guest_client, invite="wrong"),
        ]

        for response in pages:
            body = await response.text()

            assert "--accent: #4a9eff" in body, f"{response.url} has no styles"

            for token in tokens:
                assert token not in body, f"{response.url} still shows {token}"

    async def test_signed_in_user_cannot_register(
        self,
        client,
        users,
        with_signup,
    ):
        """Both the page and the form.

        An existing session must not be quietly swapped for the account
        someone is about to create — signing out first is what makes that
        swap deliberate.
        """
        page = await client.get("/signup", allow_redirects=False)

        assert page.status == 303
        assert page.headers["Location"] == "/"

        form = await _signup(client, username="second")

        assert form.status == 303
        assert form.headers["Location"] == "/"
        # Only the account the client fixture signed in as.
        assert users.count() == 1

    def test_empty_code_never_opens_the_door(self, monkeypatch):
        """compare_digest would happily match an empty string to itself."""
        monkeypatch.setattr(Config, "SIGNUP_INVITE_CODE", "")

        assert signup_allowed("") is False


class TestSignupDisabled:
    """No invite code configured is the default, and it means no sign-up.

    Every deployment that never thought about this has to end up with the
    page absent, the route answering 404 and the login page offering nothing.

    Sign-up is pinned off here rather than assumed off: a developer's own
    `.env` can set a code, and the default under test should not depend on
    what happens to be in it.
    """

    @pytest.fixture(autouse=True)
    def signup_off(self, monkeypatch):
        monkeypatch.setattr(Config, "SIGNUP_INVITE_CODE", "")

    async def test_page_is_not_found(self, guest_client):
        response = await guest_client.get("/signup")

        assert response.status == 404

    async def test_post_creates_no_account(self, guest_client, users):
        response = await _signup(guest_client, invite="", username="intruder")

        assert response.status == 404
        assert users.count() == 0

    async def test_login_page_has_no_link(self, guest_client):
        response = await guest_client.get("/login")

        assert 'href="/signup"' not in await response.text()

    async def test_a_whitespace_only_code_counts_as_unset(
        self,
        guest_client,
        users,
        monkeypatch,
    ):
        """The gate and signup_allowed have to agree with each other.

        signup_allowed() strips the configured code, so a variable holding
        only spaces would otherwise render a page that rejects every code
        typed into it — sign-up that looks on and cannot work.
        """
        monkeypatch.setattr(Config, "SIGNUP_INVITE_CODE", "   ")

        response = await _signup(guest_client, invite="   ")

        assert response.status == 404
        assert users.count() == 0
