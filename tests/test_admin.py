"""The administrative area and the one check that stands in front of it.

The tests here are mostly about refusal, because a permission is only worth
what its refusals are worth. The three that matter to an outsider are the
last three: a stranger, a signed-in stranger, and a stranger with a cookie
they edited themselves.
"""

import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.conftest import TEST_PASSWORD, TEST_USERNAME

ADMIN_PREFIX = "/admin"
USERS_PATH = "/admin/users"


@pytest.fixture
def web_server(manager, database_with_schema, web_server_factory):
    return web_server_factory(manager, database_with_schema)


@pytest.fixture
async def new_client(web_server):
    """Build an independent client.

    `app_client` is shared with the fixtures that sign in on it, so a test
    that needs a second browser — one person's session stopping while
    another's keeps working — cannot use it.
    """
    made = []

    async def _make() -> TestClient:
        client = TestClient(TestServer(web_server.build_app()))
        await client.start_server()
        made.append(client)
        return client

    yield _make

    for client in made:
        await client.close()


@pytest.fixture
async def app_client(new_client):
    return await new_client()


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
        """`/admin` leads rather than lists, so following the address is
        what an administrator does and what a bookmark ends up at."""
        response = await admin_client.get(USERS_PATH)

        assert response.status == 200
        assert "<h1>Пользователи</h1>" in await response.text()

    async def test_the_area_itself_points_at_its_section(self, admin_client):
        response = await admin_client.get(ADMIN_PREFIX, allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == USERS_PATH

    async def test_a_signed_out_visitor_is_sent_to_sign_in(self, app_client):
        """The same answer every other protected address gives, so the owner
        can bookmark the link and come back to it."""
        response = await app_client.get(USERS_PATH, allow_redirects=False)

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
        response = await other_user_client.get(USERS_PATH)

        assert response.status == 404
        assert "Пользователи" not in await response.text()

    async def test_a_forged_cookie_changes_nothing(self, app_client):
        """The obvious hand-made request: take somebody else's cookie string
        and send it back. The session lookup is a hash of a token nobody else
        has, so this is an ordinary unknown token and nothing more.
        """
        app_client.session.cookie_jar.update_cookies(
            {"platform_session": "forged-" + "0" * 40}
        )

        response = await app_client.get(USERS_PATH, allow_redirects=False)

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
        closed = await other_user_client.get(USERS_PATH)
        missing = await other_user_client.get("/no-such-place")

        assert closed.status == missing.status == 404

    async def test_the_area_is_never_a_search_result(self, admin_client):
        """robots.txt already disallows it, so this is the second of two
        rather than the only line — but the first is one edit away from
        being wrong, and a page nobody may open should not be findable."""
        body = await (await admin_client.get(USERS_PATH)).text()

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


class TestTheUserList:
    async def test_it_lists_the_accounts(self, admin_client, create_user):
        create_user(username="hr", password=TEST_PASSWORD)

        body = await (await admin_client.get(USERS_PATH)).text()

        assert TEST_USERNAME in body
        assert "hr" in body

    async def test_a_search_narrows_it(self, admin_client, create_user):
        create_user(username="hr", password=TEST_PASSWORD)

        body = await (await admin_client.get(f"{USERS_PATH}?q=hr")).text()
        table = body[body.index("<table") : body.index("</table>")]

        assert "hr" in table
        # Not the whole page: the attic carries the name of whoever is
        # reading it, on every page now.
        assert TEST_USERNAME not in table

    async def test_a_search_that_matches_nothing_says_so(
        self, admin_client, create_user
    ):
        body = await (await admin_client.get(f"{USERS_PATH}?q=nobody")).text()

        assert "Ничего не найдено" in body

    async def test_a_wildcard_is_a_character_not_a_wildcard(
        self, admin_client, create_user
    ):
        """`%` is what LIKE treats as 'any run of characters'.

        Unescaped, a search for `%` returns the whole table and a search for
        a name containing one finds something that is not there. A search box
        that answers differently depending on the punctuation of the query is
        not a thing anybody can reason about later.
        """
        create_user(username="hr", password=TEST_PASSWORD)

        body = await (await admin_client.get(f"{USERS_PATH}?q=%25")).text()
        # Past the attic, which carries the reader's own name everywhere.
        page = body[body.index("<h1>") :]

        assert "Ничего не найдено" in page
        assert TEST_USERNAME not in page

    async def test_the_state_of_each_account_is_shown(self, admin_client, users):
        body = await (await admin_client.get(USERS_PATH)).text()

        assert "активен" in body


class TestChangingAnAccount:
    async def _token(self, client) -> str:
        body = await (await client.get(USERS_PATH)).text()
        match = re.search(r'name="csrf" value="([^"]+)"', body)
        return match.group(1)

    async def test_an_administrator_disables_an_account(
        self, admin_client, users, create_user
    ):
        create_user(username="hr", password=TEST_PASSWORD)
        token = await self._token(admin_client)
        target = users.get_by_username("hr")

        response = await admin_client.post(
            f"{USERS_PATH}/{target.id}/disable",
            data={"csrf": token},
            allow_redirects=False,
        )

        assert response.status == 303
        assert users.get_by_username("hr").is_active is False

    async def test_and_enables_it_again(
        self, admin_client, users, create_user
    ):
        create_user(username="hr", password=TEST_PASSWORD)
        target = users.get_by_username("hr")
        users.set_active(target.id, False)
        token = await self._token(admin_client)

        await admin_client.post(
            f"{USERS_PATH}/{target.id}/enable",
            data={"csrf": token},
            allow_redirects=False,
        )

        assert users.get_by_username("hr").is_active is True

    async def test_a_disabled_account_cannot_sign_in_afterwards(
        self, admin_client, users, create_user, new_client, login
    ):
        """The point of disabling. The check is on the way in, so the existing
        session stops working on the next request without anything revoking
        it."""
        create_user(username="hr", password=TEST_PASSWORD)
        target = users.get_by_username("hr")

        theirs = await new_client()
        await login(theirs, username="hr")

        # The console answers a signed-in visitor with itself and an
        # anonymous one with the sign-in form.
        before = await theirs.get("/", allow_redirects=False)
        assert before.status == 200

        token = await self._token(admin_client)
        await admin_client.post(
            f"{USERS_PATH}/{target.id}/disable",
            data={"csrf": token},
            allow_redirects=False,
        )

        response = await theirs.get("/", allow_redirects=False)

        assert response.headers["Location"] == "/login"

    async def test_the_request_needs_a_token(
        self, admin_client, users, create_user
    ):
        """The account must survive a POST with no token, and the browser
        must land somewhere it can get a fresh one — not on a bare line of
        text, which is what a reader who signed out in another tab used to
        get for pressing a button that had worked a minute earlier."""
        create_user(username="hr", password=TEST_PASSWORD)
        target = users.get_by_username("hr")

        response = await admin_client.post(
            f"{USERS_PATH}/{target.id}/disable",
            data={"csrf": "made-up"},
            allow_redirects=False,
        )

        assert response.status == 303
        assert response.headers["Location"] == USERS_PATH
        assert users.get_by_username("hr").is_active is True

    async def test_an_administrator_cannot_disable_themselves(
        self, admin_client, users, create_user
    ):
        """Otherwise the only place that could undo it is the one that was
        just locked. The flag survives, so the host command is the way back."""
        create_user(username="hr", password=TEST_PASSWORD)
        token = await self._token(admin_client)
        me = users.get_by_username(TEST_USERNAME)

        await admin_client.post(
            f"{USERS_PATH}/{me.id}/disable",
            data={"csrf": token},
            allow_redirects=False,
        )

        assert users.get_by_username(TEST_USERNAME).is_active is True

    async def test_an_id_that_is_not_a_number_never_reaches_a_handler(
        self, admin_client
    ):
        """Matched in the route itself, so a hand-made request with a name
        where a number belongs stops before anything is looked up."""
        for path in ("/admin/users/alexey/disable", "/admin/users/1x/disable"):
            response = await admin_client.post(path, allow_redirects=False)

            assert response.status == 404

    async def test_a_stranger_cannot_reach_the_actions_at_all(
        self, other_user_client, users
    ):
        target = users.get_by_username("hr")

        response = await other_user_client.post(
            f"{USERS_PATH}/{target.id}/disable",
            data={"csrf": "anything"},
            allow_redirects=False,
        )

        assert response.status == 404
        assert users.get_by_username("hr").is_active is True


class TestTheVisitLog:
    async def test_a_public_page_is_recorded(self, app_client, database_with_schema):
        from app.db.visits_repo import VisitsRepository

        await app_client.get("/about")

        repo = VisitsRepository(database_with_schema)
        found = repo.recent()

        assert [v.path for v in found] == ["/about"]
        assert found[0].kind == "view"
        assert found[0].identity is None

    async def test_a_demonstration_page_is_recorded_with_its_guest(
        self, app_client, database_with_schema, pages_manager
    ):
        from app.db.visits_repo import VisitsRepository

        await app_client.get("/demo/expenses")

        found = VisitsRepository(database_with_schema).recent()

        assert found[0].path == "/demo/expenses"
        assert found[0].identity.startswith("web:guest:")

    async def test_the_landing_is_recorded_without_a_name(
        self, app_client, database_with_schema
    ):
        """The landing mints no guest: it only lists what there is to look
        at, and a namespace for somebody who never clicked would be a row per
        passer-by for nothing. So the visit is real and the reader is not
        named — which is what an unnamed row in a log is for.
        """
        from app.db.visits_repo import VisitsRepository

        await app_client.get("/demo")

        found = VisitsRepository(database_with_schema).recent()

        assert found[0].path == "/demo"
        assert found[0].identity is None

    async def test_a_page_that_nobody_may_see_is_not(
        self, admin_client, database_with_schema
    ):
        """The console is where the owner works. Logging it would bury
        everything else and teach nothing."""
        from app.db.visits_repo import VisitsRepository

        await admin_client.get(USERS_PATH)

        paths = [v.path for v in VisitsRepository(database_with_schema).recent()]

        assert USERS_PATH not in paths

    async def test_a_request_that_answered_nothing_is_not_recorded(
        self, other_user_client, database_with_schema
    ):
        from app.db.visits_repo import VisitsRepository

        await other_user_client.get(ADMIN_PREFIX)

        paths = [v.path for v in VisitsRepository(database_with_schema).recent()]

        assert ADMIN_PREFIX not in paths

    async def test_a_sign_in_is_recorded(
        self, new_client, login, create_user, database_with_schema
    ):
        from app.db.visits_repo import SIGNIN, VisitsRepository

        theirs = await new_client()
        await login(theirs)

        found = VisitsRepository(database_with_schema).recent()

        assert any(v.kind == SIGNIN for v in found)
        # The identity of an account is `web:<name>` — the same string its
        # data is filed under, not a row id.
        assert any(v.identity == f"web:{TEST_USERNAME}" for v in found)

    async def test_the_administrator_sees_them(self, admin_client):
        body = await (await admin_client.get("/admin/visits")).text()

        assert "<h1>Визиты</h1>" in body

    async def test_the_filter_narrows_to_one_page(
        self, app_client, admin_client, database_with_schema
    ):
        from app.db.visits_repo import VisitsRepository

        await app_client.get("/about")
        await app_client.get("/project")

        narrowed = VisitsRepository(database_with_schema).recent(path="/about")

        assert [v.path for v in narrowed] == ["/about"]

        body = await (await admin_client.get("/admin/visits?q=%2Fabout")).text()
        table = body[body.index("<table") : body.index("</table>")]

        # The datalist beside the filter still offers every page anyone has
        # looked at, `/project` included; what the table must not carry is a
        # row for it.
        assert "/project" in body
        assert "/project" not in table
        assert "/about" in table
        assert "на этой странице" in body

    async def test_an_empty_log_says_so(self, admin_client):
        """A filter that matches nothing still has to render as a page
        rather than as a broken table."""
        body = await (
            await admin_client.get("/admin/visits?q=/nothing-here")
        ).text()

        assert "Пока ничего не записано" in body

    async def test_a_stranger_cannot_read_it(self, other_user_client):
        response = await other_user_client.get("/admin/visits")

        assert response.status == 404

    async def test_pruning_drops_what_is_past_the_window(
        self, database_with_schema
    ):
        """The table only ever grows until something removes from it, and the
        only thing that does is this."""
        from datetime import datetime, timedelta

        from app.db.visits_repo import VisitsRepository

        repo = VisitsRepository(database_with_schema)

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO visits (at, kind, path) "
                    "VALUES (%s, 'view', '/from-long-ago')",
                    (datetime.now() - timedelta(days=200),),
                )
            conn.commit()

        repo.record("view", path="/about")
        assert repo.count() == 2

        # A hundred days keeps what is a minute old and drops what is not.
        assert repo.prune(days=100) == 1

        left = [v.path for v in repo.recent()]
        assert left == ["/about"]

    async def test_our_own_activity_does_not_fill_the_log(
        self, admin_client, database_with_schema
    ):
        """Signing in to look at the log is the most likely thing to happen on
        a quiet day, and it must not be what fills it."""
        from app.db.visits_repo import VisitsRepository

        await admin_client.get("/admin/visits")

        paths = [v.path for v in VisitsRepository(database_with_schema).recent()]

        assert ADMIN_PREFIX not in paths


class TestPruningFromTheHost:
    def test_the_command_removes_what_aged_out(
        self, database_with_schema, create_user
    ):
        """The one thing that takes rows out of the table."""
        from datetime import datetime, timedelta

        from app.db.visits_repo import VisitsRepository
        from app.manage import main

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO visits (at, kind, path) "
                    "VALUES (%s, 'view', '/ancient')",
                    (datetime.now() - timedelta(days=400),),
                )
            conn.commit()

        assert main(["prune-visits"]) == 0

        assert VisitsRepository(database_with_schema).recent() == []


class TestHidingTheAdministrator:
    """The owner's own checking is recorded here like a stranger's.

    Nothing in the log says which is which — no address is kept — so the page
    cannot tell them apart either. This is the page deciding not to show them.
    """

    async def test_it_hides_the_administrators_own_events(
        self, admin_client, new_client, database_with_schema
    ):
        from app.db.visits_repo import VisitsRepository

        theirs = await new_client()
        await theirs.get("/about")

        VisitsRepository(database_with_schema).record(
            "view", path="/project", identity=f"web:{TEST_USERNAME}"
        )

        # Past the attic, which carries the reader's own name everywhere.
        body = await (await admin_client.get("/admin/visits")).text()
        assert TEST_USERNAME in body[body.index("<h1>") :]

        hidden = await (await admin_client.get("/admin/visits?hide=1")).text()

        page = hidden[hidden.index("<h1>") :]
        table = page[page.index("<table") : page.index("</table>")] if (
            "<table" in page
        ) else ""

        assert TEST_USERNAME not in page
        # The path still sits in the filter's suggestions — those list every
        # page anyone has looked at, which is the point of them. What has to
        # go is the row.
        assert "/project" in hidden
        assert "/project" not in table

    async def test_it_keeps_everybody_elses(self, admin_client, database_with_schema):
        from app.db.visits_repo import VisitsRepository

        repo = VisitsRepository(database_with_schema)
        repo.record("view", path="/demo/expenses", identity="web:guest:someone")
        repo.record("view", path="/demo/expenses",
                    identity=f"web:{TEST_USERNAME}")

        hidden = await (await admin_client.get("/admin/visits?hide=1")).text()

        assert "web:guest:someone" in hidden

    async def test_no_placeholder_reaches_the_browser(self, admin_client):
        """A forgotten substitution shows as literal text.

        The count line is composed in Python and its numbers are filled in
        afterwards, so the two have to travel together through the same
        substitution. They did not, once, and the page said __TOTAL__.
        """
        for suffix in ("", "?hide=1", "?q=/about"):
            body = await (await admin_client.get(f"/admin/visits{suffix}")).text()

            assert "__" not in body, suffix

    async def test_the_box_filters_without_pressing_the_button(self, admin_client):
        """It is a filter, so it filters when it moves. The form is a GET and
        the button stays for a browser with script off."""
        body = await (await admin_client.get("/admin/visits")).text()

        assert 'addEventListener("change"' in body
        assert "this.form.submit()" in body
        assert '<button type="submit">Найти</button>' in body

    async def test_the_box_stays_ticked(self, admin_client):
        body = await (await admin_client.get("/admin/visits?hide=1")).text()

        assert 'name="hide" value="1" checked' in body

    async def test_unticking_brings_them_back(self, admin_client, database_with_schema):
        from app.db.visits_repo import VisitsRepository

        VisitsRepository(database_with_schema).record(
            "view", path="/project", identity=f"web:{TEST_USERNAME}"
        )

        body = await (await admin_client.get("/admin/visits")).text()

        assert TEST_USERNAME in body[body.index("<h1>") :]

    async def test_the_count_says_what_it_is_counting(
        self, admin_client, database_with_schema
    ):
        """A total nobody can match against the table under it is a thing to
        wonder about every time."""
        from app.db.visits_repo import VisitsRepository

        VisitsRepository(database_with_schema).record(
            "view", path="/project", identity=f"web:{TEST_USERNAME}"
        )

        shown = await (await admin_client.get("/admin/visits?hide=1")).text()
        everything = await (await admin_client.get("/admin/visits")).text()

        assert "Показано: <b>0</b>" in shown
        assert "Всего событий: <b>" in everything
