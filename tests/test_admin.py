"""The administrative area and the one check that stands in front of it.

The tests here are mostly about refusal, because a permission is only worth
what its refusals are worth. The three that matter to an outsider are the
last three: a stranger, a signed-in stranger, and a stranger with a cookie
they edited themselves.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.web.server import WebServer
from tests.conftest import TEST_PASSWORD, TEST_USERNAME

ADMIN_PREFIX = "/admin"


@pytest.fixture
def web_server(manager, database_with_schema):
    return WebServer(
        manager=manager,
        database=database_with_schema,
        host="127.0.0.1",
        port=0,
    )


@pytest.fixture
async def app_client(web_server):
    server = TestServer(web_server.build_app())
    async with TestClient(server) as client:
        yield client


@pytest.fixture
async def signed_in(app_client, login):
    await login(app_client)
    return app_client


@pytest.fixture
async def admin_client(app_client, login, create_user, users):
    """Signed in as the default account, holding the flag."""
    account = users.get_by_username(TEST_USERNAME)
    users.set_admin(account.id, True)

    await login(app_client)
    return app_client


@pytest.fixture
async def other_user_client(app_client, login, create_user, users):
    """Signed in as somebody who is not the administrator.

    The second account matters: a test where the only user is the one holding
    the flag cannot tell "refused because not an admin" apart from "refused
    because nobody is signed in at all".
    """
    create_user(username="hr", password=TEST_PASSWORD)
    await login(app_client, username="hr")
    return app_client


class TestTheFlag:
    async def test_a_new_account_is_not_an_administrator(
        self, app_client, login, create_user, users
    ):
        account = users.get_by_username(TEST_USERNAME)

        assert account.is_admin is False

    async def test_granting_takes_effect_without_a_new_session(
        self, app_client, login, create_user, users
    ):
        """The flag is read from the database on the way in, so revoking it
        closes the door immediately rather than at the next sign-in.

        A flag copied into the session would keep the door open until the
        session expired, which is the difference between a way to lock
        yourself out on purpose and a way that only works if you wait.
        """
        account = users.get_by_username(TEST_USERNAME)
        users.set_admin(account.id, True)

        await login(app_client)

        assert (await app_client.get(ADMIN_PREFIX)).status == 200

        users.set_admin(account.id, False)

        assert (await app_client.get(ADMIN_PREFIX)).status == 404


class TestTheGate:
    async def test_an_administrator_reaches_the_area(self, admin_client):
        response = await admin_client.get(ADMIN_PREFIX)

        assert response.status == 200
        assert "Служебный раздел" in await response.text()

    async def test_a_signed_out_visitor_is_sent_to_sign_in(self, app_client):
        """The same answer every other protected address gives, so the owner
        can bookmark the link and come back to it."""
        response = await app_client.get(ADMIN_PREFIX, allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == "/login"

    async def test_a_signed_in_stranger_gets_a_missing_page(
        self, other_user_client
    ):
        """A 404 and not a 403.

        A 403 says the address exists and only the permission is missing,
        which is an answer a stranger does not need and which turns every
        extra route here into another thing to enumerate.
        """
        response = await other_user_client.get(ADMIN_PREFIX)

        assert response.status == 404
        assert "Служебный раздел" not in await response.text()

    async def test_a_forged_cookie_changes_nothing(self, app_client):
        """The obvious hand-made request: take somebody else's cookie string
        and send it back. The session lookup is a hash of a token nobody else
        has, so this is an ordinary unknown token and nothing more.
        """
        app_client.session.cookie_jar.update_cookies(
            {"platform_session": "forged-" + "0" * 40}
        )

        response = await app_client.get(ADMIN_PREFIX, allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == "/login"

    async def test_a_path_that_does_not_exist_looks_the_same(
        self, other_user_client
    ):
        """An address that was never registered and one that is closed must
        answer alike, or the difference tells a stranger where the
        administration lives.

        The status is what this asserts. The body is not claimed to match:
        this application has no missing-page template for the two to match,
        and a Russian heading on one of them is not what a request author
        is reading.
        """
        closed = await other_user_client.get(ADMIN_PREFIX)
        missing = await other_user_client.get("/no-such-place")

        assert closed.status == missing.status == 404

    async def test_the_area_is_never_a_search_result(self, admin_client):
        """robots.txt already disallows it, so this is the second of two
        rather than the only line — but the first is one edit away from
        being wrong, and a page nobody may open should not be findable."""
        body = await (await admin_client.get(ADMIN_PREFIX)).text()

        assert 'name="robots" content="noindex' in body


class TestTheMenu:
    async def test_the_administrator_is_offered_the_link(self, admin_client):
        body = await (await admin_client.get("/")).text()

        assert f'href="{ADMIN_PREFIX}"' in body

    async def test_a_stranger_is_not(self, other_user_client):
        body = await (await other_user_client.get("/")).text()

        assert f'href="{ADMIN_PREFIX}"' not in body

    async def test_a_guest_is_not(self, app_client):
        body = await (await app_client.get("/login")).text()

        assert f'href="{ADMIN_PREFIX}"' not in body


class TestTheCommand:
    """Granting the flag from the host, which is the only way to get it."""

    @pytest.fixture
    def cli(self, database_with_schema, create_user):
        from app.manage import main

        def _run(*argv: str) -> int:
            return main(list(argv))

        return _run

    @pytest.fixture
    def repo(self, database_with_schema):
        from app.db.users_repo import UsersRepository

        return UsersRepository(database_with_schema)

    def test_it_grants_the_flag(self, cli, repo):
        assert cli("grant-admin", TEST_USERNAME) == 0

        assert repo.get_by_username(TEST_USERNAME).is_admin is True

    def test_it_takes_the_flag_back(self, cli, repo):
        cli("grant-admin", TEST_USERNAME)

        assert cli("revoke-admin", TEST_USERNAME) == 0

        assert repo.get_by_username(TEST_USERNAME).is_admin is False

    def test_granting_twice_is_not_an_error(self, cli, repo):
        cli("grant-admin", TEST_USERNAME)

        assert cli("grant-admin", TEST_USERNAME) == 0

    def test_an_unknown_account_fails_rather_than_creating_one(self, cli, repo):
        assert cli("grant-admin", "nobody") == 1

        assert repo.get_by_username("nobody") is None

    def test_leaving_sessions_alone_is_deliberate(self, cli, repo):
        """The flag is read from the database on the way in, so there is
        nothing to revoke and nothing that would outlive the change."""
        cli("grant-admin", TEST_USERNAME)

        assert repo.get_by_username(TEST_USERNAME).is_admin is True
