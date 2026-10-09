"""Linking a web account to a Telegram chat from the browser.

Three things are being tested here. The button appears only when the
deployment can actually use it. A verified payload links the account it was
verified under, and only under a nonce the same browser holds. And a payload
that fails any check changes nothing.
"""

import hashlib
import hmac
import re
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.db.links_repo import TelegramLinksRepository
from app.db.users_repo import UsersRepository
from app.web.server import LINK_NONCE_COOKIE, WebServer
from tests.conftest import TEST_PASSWORD, TEST_USERNAME

BOT_TOKEN = "123456:ABC-DEF_token_for_tests"
BOT_USERNAME = "example_bot"
CHAT_ID = 4242

LINK_PATH = "/account/telegram/link"


@pytest.fixture
def web_server(manager, database_with_schema):
    return WebServer(
        manager=manager,
        database=database_with_schema,
        host="127.0.0.1",
        port=0,
    )


@pytest.fixture(autouse=True)
def linking_enabled(monkeypatch):
    """Linking on, unless a test says otherwise.

    Config is evaluated at import time, so the only way to vary it is to set
    the attribute for the duration of a test.
    """
    monkeypatch.setattr(Config, "BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setattr(Config, "TELEGRAM_BOT_USERNAME", BOT_USERNAME)


@pytest.fixture
def links(database_with_schema):
    return TelegramLinksRepository(database_with_schema)


@pytest.fixture
def users(database_with_schema):
    return UsersRepository(database_with_schema)


@pytest.fixture
async def signed_in(web_server, create_user, login):
    test_server = TestServer(web_server.build_app())
    async with TestClient(test_server) as test_client:
        await login(test_client)
        yield test_client


@pytest.fixture
async def guest(web_server):
    """A browser with no session."""
    test_server = TestServer(web_server.build_app())
    async with TestClient(test_server) as test_client:
        yield test_client


def payload(token: str = BOT_TOKEN, **overrides) -> dict[str, str]:
    """A payload Telegram would have signed, by hand."""
    data = {
        "id": str(CHAT_ID),
        "first_name": "Alexey",
        "username": "alexey",
        "auth_date": str(int(time.time())),
    }
    data.update(overrides)

    check_string = "\n".join(
        f"{key}={data[key]}" for key in sorted(data) if key != "hash"
    )

    data["hash"] = hmac.new(
        hashlib.sha256(token.encode()).digest(),
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    return data


async def nonce_from(client: TestClient) -> str:
    """Read the nonce the page just handed to the widget."""
    page = await (await client.get("/")).text()

    match = re.search(r"data-auth-url=\"/account/telegram/link\?nonce=([^\"]+)\"", page)

    assert match, "the page offered no link button, so there is no nonce"

    return match.group(1)


async def link_as(client: TestClient, token: str = BOT_TOKEN, **overrides):
    """Do what the widget does: fetch the page, then follow the redirect.

    Fetches a nonce of its own every time, so two calls are two independent
    attempts. Reusing one nonce is a separate test, because the point of it is
    that the second call is supposed to fail.
    """
    nonce = await nonce_from(client)

    return await client.get(
        LINK_PATH,
        params={**payload(token, **overrides), "nonce": nonce},
        allow_redirects=False,
    )


class TestTheButton:
    async def test_offers_linking_when_nothing_is_linked(self, signed_in):
        body = await (await signed_in.get("/")).text()

        assert BOT_USERNAME in body
        assert "Link Telegram" in body

    async def test_the_button_carries_a_nonce(self, signed_in):
        """Without one, anyone could have their browser submit a payload."""
        assert await nonce_from(signed_in)

    async def test_offers_unlinking_once_linked(self, signed_in, links, users):
        user = users.get_by_username(TEST_USERNAME)
        links.link(user.id, CHAT_ID)

        body = await (await signed_in.get("/")).text()

        assert str(CHAT_ID) in body
        assert "Unlink Telegram" in body
        assert "Link Telegram" not in body

    async def test_a_linked_page_hands_out_no_nonce(self, signed_in, links, users):
        """No second nonce while already linked: nothing to link."""
        links.link(users.get_by_username(TEST_USERNAME).id, CHAT_ID)

        body = await (await signed_in.get("/")).text()

        assert LINK_NONCE_COOKIE not in body

    async def test_hidden_when_no_bot_username(self, signed_in, monkeypatch):
        monkeypatch.setattr(Config, "TELEGRAM_BOT_USERNAME", "")

        body = await (await signed_in.get("/")).text()

        assert "Link Telegram" not in body
        assert "not configured" in body

    async def test_hidden_when_no_bot_token(self, signed_in, monkeypatch):
        """A button that cannot verify anything is worse than none."""
        monkeypatch.setattr(Config, "BOT_TOKEN", "")

        body = await (await signed_in.get("/")).text()

        assert "Link Telegram" not in body
        assert "not configured" in body

    async def test_the_page_leaves_no_placeholder(self, signed_in):
        for path in ("/",):
            body = await (await signed_in.get(path)).text()

            assert "__" not in body.replace("__pycache__", ""), path

    async def test_a_guest_is_sent_to_login_rather_than_offered_a_button(
        self,
        guest,
    ):
        response = await guest.get("/", allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == "/login"


class TestLinking:
    async def test_a_verified_payload_links_the_account(
        self,
        signed_in,
        links,
        users,
    ):
        response = await link_as(signed_in)

        assert response.status == 303

        user = users.get_by_username(TEST_USERNAME)
        assert links.get_by_user_id(user.id).telegram_id == CHAT_ID

    async def test_it_links_the_signed_in_account_not_the_named_one(
        self,
        signed_in,
        links,
        users,
        database_with_schema,
    ):
        """The payload says who signed; the session says whose account.

        These are different facts and mixing them up would let anyone link
        their Telegram to somebody else's account.
        """
        UsersRepository(database_with_schema).create("bob", TEST_PASSWORD)

        await link_as(signed_in)

        alexey = links.get_by_telegram_id(CHAT_ID)

        assert alexey.user_id == users.get_by_username(TEST_USERNAME).id
        assert alexey.user_id != users.get_by_username("bob").id

    async def test_a_signed_out_browser_cannot_link_anything(self, guest, links):
        response = await guest.get(LINK_PATH, allow_redirects=False)

        assert response.status == 303
        assert links.list_all() == []


class TestNonce:
    async def test_a_missing_nonce_is_refused(self, signed_in, links):
        response = await signed_in.get(LINK_PATH, params=payload())

        assert response.status == 400
        assert links.list_all() == []

    async def test_a_guessed_nonce_is_refused(self, signed_in, links):
        response = await signed_in.get(
            LINK_PATH,
            params={**payload(), "nonce": "made-up-nonce"},
        )

        assert response.status == 400
        assert links.list_all() == []

    async def test_the_same_nonce_cannot_be_reused(self, signed_in, links):
        """One attempt per nonce, so a captured URL is good once."""
        nonce = await nonce_from(signed_in)
        params = {**payload(id="5555"), "nonce": nonce}

        first = await signed_in.get(LINK_PATH, params=params, allow_redirects=False)
        second = await signed_in.get(
            LINK_PATH,
            params={**payload(id="6666"), "nonce": nonce},
            allow_redirects=False,
        )

        assert first.status == 303
        assert second.status == 400

        # The replay must not have linked the chat the second attempt carried,
        # not merely reported an error about it.
        assert links.get_by_telegram_id(6666) is None

    async def test_another_browsers_nonce_does_not_work(
        self,
        signed_in,
        web_server,
        create_user,
        login,
        links,
    ):
        """The attack this exists to stop, stated as a test.

        Someone obtains their own valid payload, then gets a different browser
        to submit it. The signature is genuine and the age is fine; only the
        nonce says no.
        """
        attacker_nonce = await nonce_from(signed_in)

        # A second browser, signed in to the same account: the attacker only
        # needs someone to open their URL, not a different person to log in.
        other_server = TestServer(web_server.build_app())
        async with TestClient(other_server) as victim:
            await login(victim)
            victim_nonce = await nonce_from(victim)

            assert victim_nonce != attacker_nonce

            response = await victim.get(
                LINK_PATH,
                params={**payload(), "nonce": attacker_nonce},
                allow_redirects=False,
            )

        assert response.status == 400
        assert links.list_all() == []

    async def test_the_nonce_lives_in_a_cookie_the_page_script_cannot_read(
        self,
        signed_in,
    ):
        """HttpOnly: the value is in the URL the widget is given, and a page
        able to read it back could forge a link from any other session."""
        response = await signed_in.get("/")

        cookies = [
            morsel
            for morsel in response.cookies.values()
            if morsel.key == LINK_NONCE_COOKIE
        ]

        assert cookies, "the page set no nonce cookie"
        assert cookies[0]["httponly"]


class TestRejectedPayloads:
    async def test_a_tampered_id_is_refused(self, signed_in, links):
        nonce = await nonce_from(signed_in)
        forged = payload()
        forged["id"] = str(CHAT_ID + 1)

        response = await signed_in.get(
            LINK_PATH,
            params={**forged, "nonce": nonce},
        )

        assert response.status == 400
        assert links.list_all() == []

    async def test_a_payload_signed_by_another_bot_is_refused(self, signed_in, links):
        response = await link_as(signed_in, token="999:another-bot-token")

        assert response.status == 400
        assert links.list_all() == []

    async def test_an_expired_payload_is_refused(self, signed_in, links):
        stale = int(time.time()) - Config.TELEGRAM_LOGIN_MAX_AGE_SECONDS - 60

        response = await link_as(signed_in, auth_date=str(stale))

        assert response.status == 400
        assert links.list_all() == []

    async def test_a_rejection_explains_itself_on_the_page(self, signed_in):
        """Silent failure would look like the button not working."""
        response = await link_as(signed_in, token="wrong")

        assert response.status == 400
        assert "Could not verify" in await response.text()


class TestOneAccountOneChat:
    async def test_a_chat_already_held_by_another_is_refused(
        self,
        signed_in,
        links,
        users,
        database_with_schema,
    ):
        other = UsersRepository(database_with_schema).create("bob", TEST_PASSWORD)
        links.link(other.id, CHAT_ID)

        response = await link_as(signed_in)

        assert response.status == 400
        assert links.get_by_telegram_id(CHAT_ID).user_id == other.id

    async def test_the_conflict_names_the_other_account(self, signed_in, links, users):
        other = UsersRepository(links.database).create("bob", TEST_PASSWORD)
        links.link(other.id, CHAT_ID)

        body = await (await link_as(signed_in)).text()

        assert "bob" in body

    async def test_relinking_your_own_account_to_a_new_chat_works(
        self,
        signed_in,
        links,
        users,
    ):
        user = users.get_by_username(TEST_USERNAME)

        # The nonce is taken while the page still offers the button: once
        # linked, there is nothing to press, and a nonce held from before then
        # is exactly the stale page this is meant to simulate.
        nonce = await nonce_from(signed_in)
        links.link(user.id, CHAT_ID)

        response = await signed_in.get(
            LINK_PATH,
            params={**payload(id="9999"), "nonce": nonce},
            allow_redirects=False,
        )

        assert response.status == 303
        assert links.get_by_user_id(user.id).telegram_id == 9999
        assert links.get_by_telegram_id(CHAT_ID) is None

    async def test_relinking_to_the_same_chat_changes_nothing(
        self,
        signed_in,
        links,
        users,
    ):
        user = users.get_by_username(TEST_USERNAME)
        nonce = await nonce_from(signed_in)
        links.link(user.id, CHAT_ID)

        response = await signed_in.get(
            LINK_PATH,
            params={**payload(), "nonce": nonce},
            allow_redirects=False,
        )

        assert response.status == 303
        assert links.get_by_user_id(user.id).telegram_id == CHAT_ID


class TestUnlinking:
    async def test_the_button_removes_the_link(self, signed_in, links, users):
        user = users.get_by_username(TEST_USERNAME)
        links.link(user.id, CHAT_ID)

        response = await signed_in.post(
            "/account/telegram/unlink",
            allow_redirects=False,
        )

        assert response.status == 303
        assert links.get_by_user_id(user.id) is None

    async def test_the_offers_to_link_again_afterwards(self, signed_in, links, users):
        user = users.get_by_username(TEST_USERNAME)
        links.link(user.id, CHAT_ID)

        await signed_in.post("/account/telegram/unlink", allow_redirects=False)
        body = await (await signed_in.get("/")).text()

        assert "Link Telegram" in body

    async def test_unlinking_without_a_link_is_harmless(self, signed_in, links):
        response = await signed_in.post(
            "/account/telegram/unlink",
            allow_redirects=False,
        )

        assert response.status == 303
        assert links.list_all() == []

    async def test_it_does_not_remove_someone_elses_link(
        self,
        signed_in,
        links,
        users,
        database_with_schema,
    ):
        other = UsersRepository(database_with_schema).create("bob", TEST_PASSWORD)
        links.link(other.id, CHAT_ID)

        await signed_in.post("/account/telegram/unlink", allow_redirects=False)

        assert links.get_by_user_id(other.id).telegram_id == CHAT_ID

    async def test_a_guest_cannot_unlink(self, guest, links, users):
        other = UsersRepository(links.database).create("bob", TEST_PASSWORD)
        links.link(other.id, CHAT_ID)

        response = await guest.post(
            "/account/telegram/unlink",
            allow_redirects=False,
        )

        assert response.status == 303
        assert links.get_by_user_id(other.id) is not None


class TestLinkingDisabled:
    async def test_the_route_is_gone_when_not_configured(
        self,
        signed_in,
        monkeypatch,
    ):
        """Not merely refused: a disabled deployment has no such path."""
        monkeypatch.setattr(Config, "TELEGRAM_BOT_USERNAME", "")

        response = await signed_in.get(LINK_PATH, params=payload())

        assert response.status == 404
