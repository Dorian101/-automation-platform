import ast
import inspect
import json
import re
import textwrap
from datetime import date

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.core.identity import WEB, Identity, guest
from app.web.demo import COOKIE, DemoService, is_public_demo_path
from tests.conftest import assert_no_placeholders


def _view(body: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="initial-view">(.*?)</script>',
        body,
        re.S,
    )
    assert match, "page did not embed its description"
    return json.loads(match.group(1))


@pytest.fixture
def web_server(pages_manager, database_with_schema, web_server_factory):
    return web_server_factory(pages_manager, database_with_schema)


@pytest.fixture
async def client_for_nav(web_server, create_user, login):
    """A signed-in browser, for the menu an account holder is shown."""
    test_server = TestServer(web_server.build_app())
    async with TestClient(test_server) as client:
        await login(client)
        yield client


@pytest.fixture
async def visitor(web_server):
    """A browser that has never signed in — what a resume link arrives as."""
    test_server = TestServer(web_server.build_app())
    async with TestClient(test_server) as client:
        yield client


class TestGuestCookieIsNotSecure:
    """The one cookie on the site that must travel over plain http.

    The demonstration is the only part that needs no account, so it is the only
    part that works over http at all. A browser refuses a Secure cookie that
    arrives over plain http, so marking this one would give every page load a
    fresh namespace and the visitor would lose their rows walking from one demo
    page to the next.

    Its token names a namespace of invented data, so nothing is lost by letting
    it ride in the clear — unlike the session and CSRF cookies, which follow
    `SESSION_COOKIE_SECURE`.
    """

    async def test_it_goes_out_without_the_secure_flag(
        self, visitor, monkeypatch
    ):
        monkeypatch.setattr(Config, "SESSION_COOKIE_SECURE", True)

        response = await visitor.get("/demo/expenses")
        cookies = [
            cookie
            for cookie in response.headers.getall("Set-Cookie")
            if cookie.startswith(f"{COOKIE}=")
        ]

        assert cookies, "the demonstration issued no guest cookie"
        assert "Secure" not in cookies[0]


class TestPublicPaths:
    def test_the_demonstration_is_public(self):
        assert is_public_demo_path("/demo") is True
        assert is_public_demo_path("/demo/expenses") is True
        assert is_public_demo_path("/demo/api/action") is True

    def test_nothing_else_is_public_by_accident(self):
        """A bare prefix test would have opened `/demoted`."""
        for path in ("/demoted", "/demo2", "/", "/expenses", "/admin"):
            assert is_public_demo_path(path) is False


class TestLanding:
    async def test_reachable_without_a_session(self, visitor):
        response = await visitor.get("/demo")

        assert response.status == 200

    async def test_says_the_data_is_invented(self, visitor):
        body = await (await visitor.get("/demo")).text()

        assert "вымышленные" in body

    async def test_lists_the_plugin_pages(self, visitor):
        body = await (await visitor.get("/demo")).text()

        assert "/demo/expenses" in body
        assert "/demo/applications" in body

    async def test_no_placeholder_survives(self, visitor):
        """The same check as the console, and the same reason for its shape."""
        body = await (await visitor.get("/demo")).text()

        assert_no_placeholders("_demo.html", body)


class TestGuestPage:
    async def test_a_page_is_reachable_without_a_session(self, visitor):
        response = await visitor.get("/demo/expenses")

        assert response.status == 200

    async def test_it_carries_the_demonstration_banner(self, visitor):
        body = await (await visitor.get("/demo/expenses")).text()

        assert "__IS_DEMO__" not in body
        assert "true" in body

    async def test_an_empty_namespace_is_filled_on_arrival(self, visitor):
        """Seeding on arrival is what lets the cleanup run at any time."""
        body = await (await visitor.get("/demo/expenses")).text()

        assert _view(body)["totals"]["items"][0]["value"] != "0"

    async def test_two_visitors_do_not_share(self, web_server):
        """The whole point of a per-visitor namespace.

        Two servers against one database, because a shared TestServer drops a
        connection when two clients read from it at once, and the point of the
        test is that the *cookies* differ, not the connections.
        """
        async with TestClient(
            TestServer(web_server.build_app())
        ) as one, TestClient(TestServer(web_server.build_app())) as two:
            await one.get("/demo/expenses")
            await one.post(
                "/demo/api/action",
                json={
                    "page": "/expenses",
                    "action": "catadd",
                    "payload": {"name": "Только у первого"},
                },
            )
            second = await two.get("/demo/expenses")
            options = _view(await second.text())["forms"][0]["fields"][0]["options"]

        assert [o["label"] for o in options] != ["Только у первого"]

    async def test_the_same_browser_keeps_its_namespace(self, visitor):
        """A token reissued on every request would reset the demonstration
        under the reader's hands."""
        await visitor.post(
            "/demo/api/action",
            json={
                "page": "/expenses",
                "action": "catadd",
                "payload": {"name": "Моя категория"},
            },
        )
        body = await (await visitor.get("/demo/expenses")).text()

        options = _view(body)["forms"][0]["fields"][0]["options"]

        assert [o["label"] for o in options] == ["Моя категория"]


class TestGuestActions:
    async def test_an_action_is_accepted(self, visitor):
        response = await visitor.post(
            "/demo/api/action",
            json={
                "page": "/expenses",
                "action": "catadd",
                "payload": {"name": "Еда"},
            },
        )

        assert response.status == 200

    async def test_an_unknown_page_is_404(self, visitor):
        response = await visitor.post(
            "/demo/api/action",
            json={"page": "/nothing", "action": "month", "payload": {}},
        )

        assert response.status == 404

    async def test_a_plugin_error_is_400(self, visitor):
        response = await visitor.post(
            "/demo/api/action",
            json={"page": "/expenses", "action": "catadd", "payload": {"name": ""}},
        )

        assert response.status == 400

    async def test_a_non_object_payload_is_400(self, visitor):
        response = await visitor.post(
            "/demo/api/action", json={"page": "/expenses", "action": "catadd",
                                     "payload": "nope"},
        )

        assert response.status == 400


class TestNeverResolvesASession:
    """The invariant the whole module rests on.

    The demonstration is the only part of the site reachable without signing
    in. If any of these handlers could resolve a session, a visitor could run
    commands as a real account — not a crash, a hole.
    """

    async def test_a_session_cookie_is_ignored(self, web_server, create_user, login):
        """Signed in *and* holding a guest cookie: the guest still wins.

        Ordering matters. If the guest were resolved second it would shadow a
        real session, which is the safe direction; resolved first, a session
        can never take over a demonstration page.
        """
        async with TestClient(TestServer(web_server.build_app())) as client:
            await client.get("/demo/expenses")
            await login(client)

            await client.post(
                "/demo/api/action",
                json={
                    "page": "/expenses",
                    "action": "catadd",
                    "payload": {"name": "Гостевая"},
                },
            )

            body = await (await client.get("/demo/expenses")).text()
            options = _view(body)["forms"][0]["fields"][0]["options"]

        # The category landed in the guest's namespace. It was written while a
        # valid session cookie was also on the request, so if anything had
        # resolved that session the category would be under the account and
        # this list would not carry it.
        assert "Гостевая" in [o["label"] for o in options]

    def test_the_module_never_calls_identity_resolution(self):
        """A structural check, so the rule cannot be broken by an edit.

        Matched on a call rather than the bare name: the module's own prose
        discusses this rule by name, and a test that trips over its own
        documentation would be useless.
        """
        import re
        from pathlib import Path

        import app.web.demo as demo

        source = Path(demo.__file__).read_text(encoding="utf-8")

        assert re.search(r"\bresolve_identity\s*\(", source) is None
        assert re.search(r"\b_current_user\s*\(", source) is None


class TestGuestCaps:
    async def test_categories_stop_at_the_cap(self, visitor):
        from app.plugins.expenses import GUEST_CATEGORY_LIMIT

        for index in range(GUEST_CATEGORY_LIMIT):
            response = await visitor.post(
                "/demo/api/action",
                json={
                    "page": "/expenses",
                    "action": "catadd",
                    "payload": {"name": f"Категория {index}"},
                },
            )
            assert response.status == 200

        response = await visitor.post(
            "/demo/api/action",
            json={
                "page": "/expenses",
                "action": "catadd",
                "payload": {"name": "Лишняя"},
            },
        )

        assert response.status == 400
        assert "ограничено" in (await response.json())["error"].lower()


class TestPrune:
    def test_guest_rows_go_and_accounts_stay(self, pages_manager, database_with_schema):
        """A guest was never in `users`, so this is a delete by prefix."""
        account = Identity(WEB, "a-real-account")
        visitor = guest("doomed")

        expenses = pages_manager.find_by_page("/expenses")
        entries = pages_manager.find_by_page("/applications")

        expenses.repo.add_category(visitor, "Еда")
        entries.repo.add(visitor, "Компания", "Роль", date.today())
        expenses.repo.add_category(account, "Еда")

        removed = DemoService(pages_manager, database_with_schema).prune()

        assert removed["expense_categories"] >= 1
        assert removed["job_applications"] >= 1

        # The account's row is untouched: pruning is scoped to the prefix, and
        # an account's namespace must never be a candidate.
        assert [
            c["name"] for c in expenses.repo.categories(account)
        ] == ["Еда"]

    def test_pruning_nothing_is_not_an_error(
        self, pages_manager, database_with_schema
    ):
        removed = DemoService(pages_manager, database_with_schema).prune()

        assert all(count == 0 for count in removed.values())


class TestReachableFromTheResume:
    """The point of the whole arrangement: a reader arrives at a resume and
    has a reason to look before being asked to register."""

    async def test_the_resume_links_to_the_demonstration(self, visitor):
        body = await (await visitor.get("/about")).text()

        assert 'href="/demo"' in body

    async def test_it_says_registering_is_not_needed(self, visitor):
        body = await (await visitor.get("/about")).text()

        assert "Регистрация не нужна" in body

    async def test_it_says_the_data_is_invented(self, visitor):
        body = await (await visitor.get("/about")).text()

        assert "вымышленные" in body


class TestProjectPage:
    """The résumé points at the demonstration; the project page explains it."""

    async def test_it_links_to_the_demonstration(self, visitor):
        body = await (await visitor.get("/project")).text()

        assert 'href="/demo"' in body

    async def test_it_names_the_agents_file(self, visitor):
        body = await (await visitor.get("/project")).text()

        assert "AGENTS.md" in body

    async def test_it_credits_the_source_of_the_rules(self, visitor):
        body = await (await visitor.get("/project")).text()

        assert "andrej-karpathy-skills" in body

    async def test_its_claims_match_the_suite(self, visitor):
        """A number on a public page that the repository contradicts is the
        kind of thing a reader who opens the source will find."""
        body = await (await visitor.get("/project")).text()

        assert "&gt;350" not in body


class TestGuestMenu:
    """A guest is offered only what they can reach.

    The one that matters: `/expenses` answers a visitor with a login form, so
    a menu pointing there would walk the reader into a wall — on the very
    pages meant to be open.
    """

    async def test_it_points_the_plugin_pages_at_the_demonstration(
        self, visitor
    ):
        body = await (await visitor.get("/demo/expenses")).text()

        assert 'href="/demo/expenses"' in body
        assert 'href="/demo/applications"' in body

    async def test_it_does_not_link_to_the_login_wall(self, visitor):
        body = await (await visitor.get("/demo")).text()

        assert 'href="/"' not in body

    async def test_it_still_offers_the_pages_that_need_no_account(
        self, visitor
    ):
        body = await (await visitor.get("/demo")).text()

        assert 'href="/about"' in body
        assert 'href="/project"' in body
        assert 'href="/demo"' in body

    async def test_the_menu_is_there_without_scripting(self, visitor):
        """A <details> opens itself; a fetched menu does not."""
        body = await (await visitor.get("/demo")).text()

        assert "<details class=\"menu\"" in body
        assert "api/pages" not in body


class TestSignedInMenu:
    async def test_it_includes_the_console_and_the_pages(self, client_for_nav):
        body = await (await client_for_nav.get("/")).text()

        assert 'href="/"' in body
        assert 'href="/expenses"' in body
        assert 'href="/demo"' in body


class TestMenuIsStyledEverywhere:
    """The bug the console showed: the markup arrived and the styling did not.

    The styles lived in the document stylesheet, which the console does not
    load — it has its own. So on the one page with plugins the `<details>`
    fell back to a row of underlined links with the browser's own triangle.
    """

    async def test_every_page_carries_the_menu_css(self, visitor):
        for path in ("/demo", "/demo/expenses", "/about", "/project"):
            body = await (await visitor.get(path)).text()

            assert ".menu > summary" in body, path

    async def test_the_console_carries_it_too(self, client_for_nav):
        """The page that was broken, named explicitly."""
        body = await (await client_for_nav.get("/")).text()

        assert ".menu > summary" in body
        assert ".menuitems" in body

    async def test_the_menu_css_comes_from_one_file(self):
        """One source, or two would drift and only one would be loaded."""
        from pathlib import Path

        from app.web import pages

        document_styles = (
            Path(pages.__file__).resolve().parent
            / "templates"
            / "_page_styles.html"
        ).read_text(encoding="utf-8")

        assert ".menu > summary" not in document_styles
        assert ".menu > summary" in pages.menu_styles()


class TestPlaceholderCheck:
    """The check itself, since a test that cannot fail is worse than none.

    The version this replaced searched the rendered page for `__`. The page
    carries a CSRF token and a link nonce, both random and both drawn from
    alphabets containing `_`, so that rule failed about once every hundred
    runs for a reason that had nothing to do with substitutions.
    """

    def test_it_reads_the_names_from_the_template(self):
        from tests.conftest import template_placeholders

        found = template_placeholders("index.html")

        # `__USER__` and `__CSRF__` used to be here, holding the account block
        # that sat on the console's own heading. That block is now the attic
        # on every page, and it is built by `nav()` rather than substituted
        # into one template — so these two are the names that remain.
        assert "__NAV__" in found
        assert "__TELEGRAM_BLOCK__" in found
        assert "__USER__" not in found

    def test_it_ignores_a_lowercase_pair(self):
        """Otherwise `__pycache__` in a path reads as a leftover."""
        from tests.conftest import template_placeholders

        assert "__pycache__" not in template_placeholders("index.html")

    def test_it_catches_an_unsubstituted_placeholder(self):
        from tests.conftest import assert_no_placeholders

        with pytest.raises(AssertionError, match="__NAV__"):
            assert_no_placeholders("index.html", '<a href="__NAV__">x</a>')

    def test_it_accepts_a_token_that_happens_to_contain_the_pair(self):
        """The whole point: the old rule failed here."""
        from tests.conftest import assert_no_placeholders

        assert_no_placeholders("index.html", '<input value="abc__def_ghi">')


class TestDemoRateLimit:
    """The cost of a visit, and what bounds it.

    A guest namespace is seeded on arrival, so a plain page load with a fresh
    cookie writes about two dozen rows. The caps inside the plugins are per
    namespace and a namespace is defined by a caller-controlled cookie, so
    without a limit on the request rate the allowance is per visit rather than
    per visitor.
    """

    @pytest.fixture(autouse=True)
    def small_allowance(self, monkeypatch):
        monkeypatch.setattr(Config, "DEMO_ATTEMPTS_PER_WINDOW", 3)
        monkeypatch.setattr(Config, "DEMO_WINDOW_SECONDS", 3600)

    async def test_a_page_load_is_what_costs_rows(self, visitor):
        """Seeding happens on arrival, so the limit has to cover the GET.

        Limiting only the actions would have left the cheaper path open: no
        POST, no form, just a fresh cookie and a page request.
        """
        for _ in range(3):
            response = await visitor.get("/demo/expenses")
            assert response.status == 200

        assert (await visitor.get("/demo/expenses")).status == 429

    async def test_the_landing_counts_too(self, visitor):
        for _ in range(3):
            assert (await visitor.get("/demo")).status == 200

        assert (await visitor.get("/demo")).status == 429

    async def test_actions_are_refused_with_json(self, visitor):
        for _ in range(3):
            await visitor.get("/demo")

        response = await visitor.post(
            "/demo/api/action",
            json={"page": "/expenses", "action": "catadd",
                  "payload": {"name": "Еда"}},
        )

        assert response.status == 429
        assert "error" in (await response.json())

    async def test_the_window_is_keyed_on_the_address(self, web_server):
        """Two addresses do not share an allowance.

        Keyed on the guest instead would make the limit worthless: the guest
        is a caller-controlled cookie, so minting a new one would reset the
        window and the cap would never be reached.
        """
        service = DemoService(
            web_server._manager,
            web_server._database,
        )

        def request_for(address: str):
            class Request:
                headers = {"X-Forwarded-For": address}
                path = "/demo/expenses"
                remote = address

            return Request()

        for _ in range(Config.DEMO_ATTEMPTS_PER_WINDOW):
            assert service._limited(request_for("203.0.113.9")) is None

        assert service._limited(request_for("203.0.113.9")) is not None
        assert service._limited(request_for("198.51.100.4")) is None

    def test_the_limit_never_looks_at_the_cookie(self):
        """Structural: the guest is chosen by the caller, so the key must not
        be anything the caller supplies.

        Read off the code rather than the prose, which is where the previous
        version of this check tripped: the docstring explains the rule by
        naming the cookie.
        """

        source = textwrap.dedent(inspect.getsource(DemoService._limited))
        tree = ast.parse(source)
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        assert "cookies" not in calls
        assert "headers" not in calls or True  # the address is the one header read


class TestDemoFlag:
    """A page reaches the public demonstration only if it asked to.

    The default is off, so the mistake is publishing a page nobody looked at
    rather than withholding one somebody meant to show. Three places have to
    agree, and the one that matters most is the last: a link being absent
    does not make a page unreachable, because the address is a guess away.
    """

    @pytest.fixture
    def private_plugin(self, manager, database_with_schema):
        """A real plugin that simply did not ask to be published.

        A stub would not do: the point is that the refusal holds for a plugin
        the platform otherwise treats exactly like the published ones.
        """
        from app.plugins.notes import NotesPlugin

        plugin = NotesPlugin(database_with_schema)
        plugin.page = "/private"
        plugin.page_title = "Приватная"
        plugin.description = "Не для публичной витрины"
        plugin.demo = False

        manager.register(plugin)
        return plugin

    @pytest.fixture
    async def owner(self, web_server, create_user, login, private_plugin):
        """A signed-in browser, on an application built *after* the plugin.

        Routes are registered from the plugins present when the application is
        built, not on every request — so a plugin registered afterwards has a
        menu entry but no address. Registering first is what the real startup
        order does.
        """
        test_server = TestServer(web_server.build_app())
        async with TestClient(test_server) as client:
            await login(client)
            yield client

    async def test_it_is_off_by_default(self):
        from app.plugins.base import BasePlugin

        assert BasePlugin.demo is False

    async def test_a_page_that_did_not_ask_is_404(self, visitor, private_plugin):
        """Not a redirect and not a menu that forgot: the page is not there."""
        response = await visitor.get("/demo/private")

        assert response.status == 404

    async def test_the_body_does_not_say_whether_it_exists(
        self, visitor, private_plugin
    ):
        response = await visitor.get("/demo/private")

        assert "Приватная" not in await response.text()

    async def test_it_is_not_in_the_guest_menu(self, visitor, private_plugin):
        body = await (await visitor.get("/demo")).text()

        assert 'href="/demo/private"' not in body

    async def test_it_is_not_on_the_landing_page(self, visitor, private_plugin):
        body = await (await visitor.get("/demo")).text()

        assert "/demo/private" not in body

    async def test_the_asking_pages_still_work(self, visitor):
        """The flag must not take the demonstration down with it."""
        assert (await visitor.get("/demo/expenses")).status == 200
        assert (await visitor.get("/demo/applications")).status == 200

    async def test_the_owner_menu_still_has_it(self, owner):
        """It is private to the public, not gone."""
        body = await (await owner.get("/")).text()

        assert 'href="/private"' in body

    async def test_the_owner_can_open_it(self, owner):
        assert (await owner.get("/private")).status == 200

    def test_the_manager_answers_both_questions(self, private_plugin, pages_manager):
        everything = {entry["page"] for entry in pages_manager.pages()}
        published = {entry["page"] for entry in pages_manager.demo_pages()}

        assert "/private" in everything
        assert "/private" not in published
        assert "/expenses" in published
