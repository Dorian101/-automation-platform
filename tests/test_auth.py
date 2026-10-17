import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.web.auth import AUTH_HEADER, SESSION_COOKIE, signup_allowed
from app.web.csrf import CsrfSigner
from app.web.ratelimit import MAX_KEYS, RateLimiter
from app.web.server import WebServer
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, _hidden_field


def _texts(entries):
    """The note text alone, so assertions do not repeat the row id."""
    return [text for _, text in entries]

TOKEN = "s3cret-token-value"

INVITE_CODE = "correct-horse-invite"

WRONG_CREDENTIALS_MESSAGE = "Неверное имя пользователя или пароль."


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
    """Submit the registration form the way a browser behind the proxy would.

    A real browser loads the page first, which is where the CSRF token and its
    cookie come from. A signed-in client never reaches the form — the GET
    redirects to the console — and the POST is short-circuited the same way,
    so an absent token is fine in exactly that case.
    """
    page = await client.get("/signup", allow_redirects=False)
    csrf = await _hidden_field(page, "csrf") if page.status == 200 else ""

    return await client.post(
        "/signup",
        data={
            "invite": invite,
            "username": username,
            "password": password,
            "csrf": csrf,
        },
        allow_redirects=False,
    )


async def _login_post(
    client,
    username: str = TEST_USERNAME,
    password: str = "not-the-password",
):
    """One anonymous login attempt, with the token from a fresh form."""
    page = await client.get("/login", allow_redirects=False)

    return await client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf": await _hidden_field(page, "csrf"),
        },
        allow_redirects=False,
    )


async def _console_csrf(client) -> str:
    """The token the console bakes into its own forms."""
    page = await client.get("/", allow_redirects=False)

    return await _hidden_field(page, "csrf")


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
        response = await client.post(
            "/logout",
            data={"csrf": await _console_csrf(client)},
            allow_redirects=False,
        )

        assert response.status == 303
        assert response.headers["Location"] == "/login"

        after = await client.get("/", allow_redirects=False)

        assert after.status == 303
        assert after.headers["Location"] == "/login"

    async def test_survives_a_repeated_logout(self, client):
        await client.post(
            "/logout",
            data={"csrf": await _console_csrf(client)},
            allow_redirects=False,
        )

        # The second time there is no session left to end, and ending nothing
        # changes no state, so no token is required.
        response = await client.post("/logout", allow_redirects=False)

        assert response.status == 303


class TestCsrf:
    """Every form carries a token, tied either to the page or to the session."""

    async def test_the_login_form_hands_out_a_token(self, anonymous_client):
        body = await (await anonymous_client.get("/login")).text()

        assert 'name="csrf"' in body

    async def test_a_second_tab_does_not_break_the_first(
        self, anonymous_client
    ):
        """Loading the form twice must not invalidate the first copy.

        Double submission checks a cookie against a field. Minting a new token
        per page load kept one cookie and two forms: the second load replaced
        the cookie, so submitting the first form sent the new cookie with the
        old field and was rejected. The owner could not sign in to their own
        site with two tabs open, and the message said the form was forged.

        Both tabs, their tokens, and a submission from the *older* one.
        """
        first = await anonymous_client.get("/login")
        first_token = await _hidden_field(first, "csrf")

        second = await anonymous_client.get("/login")
        second_token = await _hidden_field(second, "csrf")

        response = await anonymous_client.post(
            "/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong on purpose",
                "csrf": first_token,
            },
            allow_redirects=False,
        )

        # 401 is the wrong password, which is the point: the token was
        # accepted. 400 with "Форма устарела" is a rejected forgery.
        assert response.status == 401, await response.text()
        assert second_token == first_token

    async def test_a_mangled_cookie_is_not_echoed_into_the_form(
        self, anonymous_client
    ):
        """A cookie that is not shaped like a token is replaced rather than
        printed, so the two halves cannot agree on rubbish and pass the check
        while proving nothing."""
        anonymous_client.session.cookie_jar.update_cookies(
            {"platform_csrf": "not-a-token"}
        )

        body = await (await anonymous_client.get("/login")).text()

        assert "not-a-token" not in body
        assert 'name="csrf"' in body

    async def test_the_signup_form_hands_out_a_token(self, signup_client):
        body = await (await signup_client.get("/signup")).text()

        assert 'name="csrf"' in body

    async def test_the_console_hands_its_forms_the_token(self, client):
        body = await (await client.get("/")).text()

        assert 'name="csrf"' in body

    async def test_a_login_without_a_token_changes_nothing(self, anonymous_client):
        response = await anonymous_client.post(
            "/login",
            data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            allow_redirects=False,
        )

        assert response.status == 400
        assert "Форма устарела" in await response.text()

        after = await anonymous_client.get("/", allow_redirects=False)

        assert after.status == 303
        assert after.headers["Location"] == "/login"

    async def test_a_logout_without_a_token_is_refused(self, client):
        """A captured session cookie alone must not end the session.

        The submitter has the cookie but not the form the page rendered, which
        is exactly the gap a cross-site request would come in through.

        Refused means the session survives, not that the reader gets a wall of
        plain text: the browser is sent to sign in, where a fresh token waits.
        """
        response = await client.post("/logout", allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == "/login"

    async def test_the_account_still_stands_after_a_refused_logout(self, client):
        await client.post("/logout", allow_redirects=False)

        after = await client.get("/", allow_redirects=False)

        assert after.status == 200


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
        assert "Инвайт-код" in await response.text()

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
        assert "Неверный инвайт-код." in await response.text()
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
        assert "не короче 8 символов" in await response.text()
        assert users.count() == 0

    async def test_blank_username_is_reported(self, signup_client, users):
        response = await _signup(signup_client, username="   ")

        assert response.status == 400
        assert "не может быть пустым" in await response.text()
        assert users.count() == 0

    async def test_taken_username_is_reported(self, signup_client, users):
        users.create(TEST_USERNAME, TEST_PASSWORD)

        response = await _signup(signup_client, username=TEST_USERNAME.upper())

        assert response.status == 409
        assert "уже занято" in await response.text()
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
            "__CSRF__",
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


class TestUsernameValidation:
    """What a username is allowed to be.

    The alphabet is enforced at the one place usernames enter the system,
    which also means a log line can only ever contain characters that belong
    to a username: an unrestricted name is a way to forge log entries.
    """

    async def test_rejects_a_control_character(self, signup_client, users):
        response = await _signup(signup_client, username="ok\nspoofed")

        assert response.status == 400
        assert users.count() == 0

    async def test_rejects_non_ascii(self, signup_client, users):
        response = await _signup(signup_client, username="джон")

        assert response.status == 400
        assert users.count() == 0

    async def test_rejects_an_overlong_username(self, signup_client, users):
        response = await _signup(signup_client, username="a" * 33)

        assert response.status == 400
        assert users.count() == 0


class TestRateLimiting:
    """The public forms stop answering after a burst of attempts.

    It is a per-process sliding window, which is all the sharing a single
    process deployment needs. The login key includes the username, so one
    account's typos do not lock out a whole network.
    """

    @pytest.fixture
    def tight_login(self, monkeypatch):
        monkeypatch.setattr(Config, "LOGIN_ATTEMPTS_PER_WINDOW", 2)
        monkeypatch.setattr(Config, "RATE_LIMIT_WINDOW_SECONDS", 60)

    async def test_login_answers_429_once_the_allowance_is_spent(
        self,
        anonymous_client,
        tight_login,
    ):
        for expected in (401, 401, 429):
            response = await _login_post(anonymous_client)
            assert response.status == expected

        assert "Попробуйте позже" in await response.text()

    async def test_a_successful_login_resets_the_count(
        self,
        secured_server,
        create_user,
        tight_login,
        login,
    ):
        """One window shared by every browser behind the proxy.

        Two clients on the same server instance see the same allowance, which
        is what clearing it on success has to prove.
        """
        async with _client(secured_server) as first:
            spent = await _login_post(first)
            assert spent.status == 401

            ok = await login(first)

        assert ok.status == 303

        async with _client(secured_server) as second:
            retry = await _login_post(second)

        assert retry.status == 401

    async def test_the_limit_is_keyed_per_username(
        self,
        anonymous_client,
        tight_login,
    ):
        for _ in range(3):
            await _login_post(anonymous_client)

        blocked = await _login_post(anonymous_client)

        assert blocked.status == 429

        other = await _login_post(anonymous_client, username="other-user")

        assert other.status == 401

    async def test_a_fresh_username_is_not_a_fresh_allowance(
        self,
        anonymous_client,
        tight_login,
        monkeypatch,
    ):
        """The window above is per username, so on its own it never runs out.

        Every attempt costs a scrypt, which blocks the event loop for a quarter
        of a second — a login form that never slows down is a way to keep the
        whole site from answering. The address window counts attempts whatever
        name they carry, so a caller that varies the name still runs out.
        """
        monkeypatch.setattr(Config, "LOGIN_ATTEMPTS_PER_ADDRESS_PER_WINDOW", 3)

        # Two spent on the first name, one on the second, and the third name is
        # refused — not because of its own window, which is empty, but because
        # the address has had its three.
        assert (await _login_post(anonymous_client)).status == 401
        assert (await _login_post(anonymous_client)).status == 401
        assert (await _login_post(anonymous_client, username="b")).status == 401

        fresh = await _login_post(anonymous_client, username="never-tried-before")

        assert fresh.status == 429

    async def test_the_address_window_leaves_room_for_the_office(
        self,
        anonymous_client,
        tight_login,
        monkeypatch,
    ):
        """A shared address must still reach the per-user windows inside it.

        The address window exists to bound the damage, not to replace the
        username in the key: several people legitimately share one proxy, and
        the reason it is not equal to the per-user window is that they do.
        """
        monkeypatch.setattr(Config, "LOGIN_ATTEMPTS_PER_ADDRESS_PER_WINDOW", 4)

        # Each user gets their own two, and the address has four to give.
        for _ in range(2):
            await _login_post(anonymous_client)
        for _ in range(2):
            await _login_post(anonymous_client, username="colleague")

        # Both personal windows are spent, and the address is spent with them.
        assert (await _login_post(anonymous_client)).status == 429
        assert (
            await _login_post(anonymous_client, username="colleague")
        ).status == 429

    async def test_a_denied_attempt_leaves_no_key_behind(
        self,
        anonymous_client,
        secured_server,
        tight_login,
    ):
        """Memory follows the attempts allowed, not the attempts made.

        Creating a window on a refusal — which is what `dict[key]` does — means a
        caller who keeps trying names they are already refused still fills the
        store, one key per name, and nothing ever empties them.
        """
        limiter = secured_server._rate

        for _ in range(4):
            await _login_post(anonymous_client, username="refused-name")

        keys = [key for key in limiter._hits if "refused-name" in key]

        # The window exists and holds the attempts that were allowed against it,
        # and the refusals did not add to it or to anything else.
        assert len(keys) == 1
        assert len(limiter._hits[keys[0]]) == 2

    async def test_the_store_stops_growing_past_its_ceiling(self):
        """The backstop for a caller that produces keys the limits do not bound."""
        limiter = RateLimiter()

        for index in range(MAX_KEYS + 50):
            limiter.allow(f"key-{index}", limit=5, window_seconds=60)

        assert len(limiter._hits) <= MAX_KEYS

    async def test_signup_is_limited_per_address(
        self,
        signup_client,
        users,
        monkeypatch,
    ):
        monkeypatch.setattr(Config, "SIGNUP_ATTEMPTS_PER_WINDOW", 1)

        first = await _signup(signup_client, username="one")

        assert first.status == 303

        # A single address gets one window, session or not: logging out and
        # trying again must still land on the limit.
        await signup_client.post(
            "/logout",
            data={"csrf": await _console_csrf(signup_client)},
            allow_redirects=False,
        )

        second = await _signup(signup_client, username="two")

        assert second.status == 429
        assert "Попробуйте позже" in await second.text()
        assert users.count() == 1


class TestSecurityHeaders:
    async def test_every_response_carries_them(self, anonymous_client):
        response = await anonymous_client.get("/login")

        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"

        csp = response.headers["Content-Security-Policy"]

        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp
        assert "https://telegram.org" in csp
        assert "form-action 'self'" in csp

    async def test_the_telegram_widgets_frame_is_allowed(self, anonymous_client):
        """The widget does not draw a button on this page.

        It inserts an iframe pointing at oauth.telegram.org. With no
        `frame-src` of its own that frame falls under `default-src 'self'`
        and is blocked, and what the reader sees is an empty box where the
        sign-in button should be — with nothing on the page to say the frame
        was refused, and no error worth reporting because the script loaded
        perfectly well.
        """
        csp = (await anonymous_client.get("/login")).headers[
            "Content-Security-Policy"
        ]

        assert "frame-src https://oauth.telegram.org" in csp

    async def test_script_src_has_no_unsafe_inline(self, anonymous_client):
        """The one directive that made an injection executable.

        Everything else in the policy can be reasoned about; this one turns any
        escaping mistake anywhere in the layer into script that runs.
        """
        csp = (await anonymous_client.get("/login")).headers[
            "Content-Security-Policy"
        ]
        script_src = csp.split("script-src", 1)[1].split(";", 1)[0]

        assert "unsafe-inline" not in script_src

    async def test_every_inline_script_carries_the_nonce_from_the_header(
        self, client
    ):
        """A nonce that no script carries blocks everything, and so does a nonce
        the header has never heard of. Both fail silently.

        The console is the strictest case: its rendering is one inline script, so
        a refused script leaves a blank page rather than a missing button.
        """
        import re

        response = await client.get("/")
        csp = response.headers["Content-Security-Policy"]
        body = await response.text()

        found = re.search(r"'nonce-([^']+)'", csp)
        assert found, f"no nonce in the policy: {csp}"

        nonce = found.group(1)
        # A script that is neither external nor a non-executing data block.
        inline = re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*\btype=)([^>]*)>", body)

        assert inline, "the page has no inline script, so this proves nothing"

        for attributes in inline:
            assert f'nonce="{nonce}"' in attributes, attributes

    async def test_the_nonce_differs_between_two_responses(self, anonymous_client):
        """Per response, not per process.

        A nonce that survived would be readable by whatever injected the script
        it guards, which is the whole thing it is for.
        """
        import re

        def nonce_of(body: str) -> str:
            return re.search(r"'nonce-([^']+)'", body).group(1)

        first = await anonymous_client.get("/login")
        second = await anonymous_client.get("/login")

        assert nonce_of(first.headers["Content-Security-Policy"]) != nonce_of(
            second.headers["Content-Security-Policy"]
        )

    async def test_no_template_carries_an_inline_script_without_asking(
        self,
    ):
        """Structural, and the reason the one above is not the whole guard.

        A template edited to add a script, and not a nonce, passes every test
        that looks at a page already rendered — and ships a page whose script
        the policy refuses. This one reads the templates directly.
        """
        import re
        from pathlib import Path

        templates = Path(__file__).resolve().parents[1] / "app/web/templates"

        for template in sorted(templates.glob("*.html")):
            source = template.read_text(encoding="utf-8")
            for attributes in re.findall(
                r"<script(?![^>]*\bsrc=)(?![^>]*\btype=)([^>]*)>", source
            ):
                assert "__CSP_NONCE__" in attributes, (
                    f"{template.name} has an inline script with no nonce: "
                    f"<script{attributes}>"
                )

    async def test_errors_carry_them_too(self, bare_client):
        response = await bare_client.get("/")

        assert response.status == 401
        assert response.headers["X-Frame-Options"] == "DENY"

    async def test_no_hsts_over_plain_http(self, anonymous_client):
        response = await anonymous_client.get("/login")

        assert "Strict-Transport-Security" not in response.headers

    async def test_hsts_when_the_deployment_is_secure(
        self,
        anonymous_client,
        monkeypatch,
    ):
        monkeypatch.setattr(Config, "SESSION_COOKIE_SECURE", True)

        response = await anonymous_client.get("/login")

        assert response.headers["Strict-Transport-Security"].startswith("max-age=")


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


class TestTokensSurviveARestart:
    """A page held open while a deploy goes out must keep working.

    The failure is entirely silent: the service came back, the page is the
    one the reader has been looking at, and the only thing that changed is
    that a form on it stopped working. Found on a sign-out button in a tab
    that was open across a restart.
    """

    def test_a_configured_secret_is_the_same_one_tomorrow(self):
        first = CsrfSigner("a-fixed-value")
        second = CsrfSigner("a-fixed-value")

        assert first.accepts("a-session", first.session_token("a-session"))
        assert second.accepts("a-session", first.session_token("a-session"))

    def test_a_token_from_before_a_restart_is_still_accepted(self):
        before = CsrfSigner("a-fixed-value")
        after = CsrfSigner("a-fixed-value")

        assert after.accepts("a-session", before.session_token("a-session"))

    def test_without_one_each_process_gets_its_own(self):
        """The fallback, and the reason the failure exists: two processes
        that were not told a secret do not agree."""
        first = CsrfSigner()
        second = CsrfSigner()

        assert not second.accepts("a-session", first.session_token("a-session"))


class TestAStaleForm:
    """A form drawn for a session that has since ended.

    Not a forgery and not a mistake: the token belongs to the session it was
    drawn for, so signing out in one tab and back in — or signing in as
    somebody else — makes every open form belong to a session that no longer
    exists. The action does not happen; that part is correct. What was wrong
    is what the reader was shown.
    """

    async def test_the_logout_button_of_a_finished_session_does_nothing(
        self, client, login
    ):
        """The token a page drew for one session, sent after the session was
        replaced by a new sign-in — the sequence that leaves a button on a
        stale tab with a token from the life before it."""
        body = await (await client.get("/")).text()
        stale = re.search(r'name="csrf" value="([^"]+)"', body).group(1)

        # Somewhere else in the browser, this session is ended and a new one
        # is started. The token above belongs to the life before.
        await client.post(
            "/logout", data={"csrf": stale}, allow_redirects=False
        )
        await login(client)

        response = await client.post(
            "/logout", data={"csrf": stale}, allow_redirects=False
        )

        assert response.status == 303
        # Refused, and the refusal meant it: the account is still signed in.
        assert (await client.get("/", allow_redirects=False)).status == 200

    async def test_it_lands_where_a_fresh_token_is_waiting(
        self, client
    ):
        response = await client.post("/logout", allow_redirects=False)

        page = await client.get(response.headers["Location"], allow_redirects=False)

        # Some page rendered, not a refusal.
        assert page.status in (200, 303)
        assert "Форма устарела" not in await response.text()
