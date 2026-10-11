"""What a search engine can find.

The site was reachable and almost invisible: the domain root redirects to the
login form, the login form linked to nothing but registration, and robots.txt
and sitemap.xml were not routes at all — they answered the same redirect. The
public pages had good titles and descriptions and no inbound link from
anywhere on the site, which is the one thing that stops a crawler finding
them.
"""

import importlib

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.web.server import INDEXABLE_PATHS, WebServer


@pytest.fixture
def web_server(pages_manager, database_with_schema):
    return WebServer(
        manager=pages_manager,
        database=database_with_schema,
        host="127.0.0.1",
        port=0,
    )


@pytest.fixture
async def crawler(web_server):
    """A visitor with no session, which is what a crawler is."""
    test_server = TestServer(web_server.build_app())
    async with TestClient(test_server) as client:
        yield client


class TestCrawlerPaths:
    async def test_robots_is_reachable_without_a_session(self, crawler):
        """It answered a redirect to the login form before.

        A crawler follows the redirect, reads a sign-in page as a robots
        file, and finds no rules in it — so the site it was meant to publish
        was never described.
        """
        response = await crawler.get("/robots.txt", allow_redirects=False)

        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/plain")

    async def test_robots_names_the_public_pages(self, crawler):
        body = await (await crawler.get("/robots.txt")).text()

        for path in INDEXABLE_PATHS:
            assert f"Allow: {path}" in body

    async def test_robots_keeps_the_rest_out(self, crawler):
        """Everything else needs a session or is the console."""
        body = await (await crawler.get("/robots.txt")).text()

        assert "Disallow: /" in body

    async def test_sitemap_is_reachable_and_well_formed(self, crawler):
        response = await crawler.get("/sitemap.xml")

        body = await response.text()

        assert response.status == 200
        assert response.headers["Content-Type"].startswith("application/xml")
        assert body.startswith('<?xml version="1.0"')
        assert "<urlset" in body
        assert body.rstrip().endswith("</urlset>")

    async def test_sitemap_lists_every_public_page(self, crawler):
        body = await (await crawler.get("/sitemap.xml")).text()

        for path in INDEXABLE_PATHS:
            assert f"<loc>https://automationplatformlebedev.ru{path}</loc>" in body

    async def test_sitemap_names_no_page_that_needs_a_session(self, crawler):
        body = await (await crawler.get("/sitemap.xml")).text()

        assert "/login" not in body
        assert "/expenses" not in body


class TestReachableFromSomewhere:
    async def test_the_login_page_links_to_the_public_pages(self, crawler):
        """The one route out of a page that only offers a form.

        The root redirects here, so a crawler starting at the domain ends up
        here. Without these links there is nothing for it to follow, and the
        pages it was meant to index are unreachable from anywhere on the site.
        """
        response = await crawler.get("/", allow_redirects=False)
        assert response.status == 303

        body = await (await crawler.get(response.headers["Location"])).text()

        assert 'href="/about"' in body
        assert 'href="/project"' in body
        assert 'href="/demo"' in body

    async def test_the_root_reaches_the_public_pages_in_two_steps(self, crawler):
        """The actual crawl: domain, then follow."""
        response = await crawler.get("/", allow_redirects=False)

        login = await crawler.get(response.headers["Location"])

        for path in INDEXABLE_PATHS:
            assert f'href="{path}"' in await login.text()


class TestPublicPages:
    async def test_the_resume_page_is_indexable(self, crawler):
        body = await (await crawler.get("/about")).text()

        assert "<title>" in body
        assert 'name="description"' in body
        assert 'name="robots"' not in body

    async def test_the_project_page_is_indexable(self, crawler):
        body = await (await crawler.get("/project")).text()

        assert "<title>" in body
        assert 'name="description"' in body
        assert 'name="robots"' not in body

    async def test_the_sign_in_page_asks_not_to_be_indexed(self, crawler):
        """It is a form, and it is the domain root after the redirect.

        A search result that lands somebody on a sign-in page is a bad
        result: better it not be offered.
        """
        body = await (await crawler.get("/login")).text()

        assert 'name="robots" content="noindex' in body

    async def test_registration_asks_not_to_be_indexed_at_all(
        self, crawler, monkeypatch
    ):
        """Configured first: without an invite code the page does not exist.

        A deployment with sign-up turned off has nothing to hide from an
        index, and the test would be asserting against a 404.
        """
        monkeypatch.setattr(Config, "SIGNUP_INVITE_CODE", "a-code")

        body = await (await crawler.get("/signup")).text()

        assert 'content="noindex, nofollow"' in body


class TestInterfaceLanguage:
    """The interface is Russian; nothing should quietly add English back.

    Sixteen tests failed on this change and every one of them was an
    assertion on a literal string — the behaviour was untouched, the words
    were. That is exactly the shape of a regression nobody notices: a new
    message arrives in the language of the code, passes every test, and ships.
    """

    # Host-facing output stays English on purpose: `app.manage` is run from a
    # shell by the operator and goes to the journal, and the rules draw the
    # line at what a person reads in a browser.
    TEMPLATES = ("index.html", "login.html", "signup.html")

    @pytest.mark.parametrize("template", TEMPLATES)
    def test_no_page_declares_english(self, template):
        from pathlib import Path

        import app.web.server as server

        source = (
            Path(server.__file__).resolve().parent / "templates" / template
        ).read_text(encoding="utf-8")

        assert '<html lang="en">' not in source

    async def test_the_console_answers_in_russian(self, web_server, create_user, login):
        from aiohttp.test_utils import TestClient as Client
        from aiohttp.test_utils import TestServer as Server

        async with Client(Server(web_server.build_app())) as client:
            await login(client)
            body = await (await client.get("/")).text()

        assert "Платформа автоматизации" in body
        assert "Доступные команды" in body
        assert "Выйти" in body

    async def test_the_sign_in_page_answers_in_russian(self, crawler):
        body = await (await crawler.get("/login")).text()

        assert "Вход в личный кабинет" in body
        assert "Имя пользователя" in body
        assert 'content="Sign in to continue."' not in body

    async def test_every_plugin_description_is_readable_to_a_person(self):
        """The descriptions are shown on the demonstration's cards.

        Two of them were still English when the interface was translated,
        and the only reason it showed was looking at the landing page rather
        than at the tests.
        """
        for module in (
            "app.plugins.notes",
            "app.plugins.clipboard",
            "app.plugins.reminders",
            "app.plugins.system",
            "app.plugins.expenses",
            "app.plugins.applications",
        ):
            loaded = importlib.import_module(module)
            # Not "the first class named *Plugin": every plugin module imports
            # BasePlugin, and alphabetically that is the first one. This
            # version picked the base, skipped it as the abstract one, and
            # never looked at a real plugin — a check that passes whatever
            # it is given. Found by putting an English description back and
            # watching the test still go green.
            plugin_class = next(
                getattr(loaded, name)
                for name in sorted(dir(loaded))
                if name.endswith("Plugin") and name != "BasePlugin"
            )

            description = plugin_class.description

            assert description, module
            assert description[0] == description[0].upper()
            assert not description.isascii(), (
                f"{module}: {description!r} is still English"
            )
