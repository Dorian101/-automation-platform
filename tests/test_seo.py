"""What a search engine can find.

The site was reachable and almost invisible: the domain root redirects to the
login form, the login form linked to nothing but registration, and robots.txt
and sitemap.xml were not routes at all — they answered the same redirect. The
public pages had good titles and descriptions and no inbound link from
anywhere on the site, which is the one thing that stops a crawler finding
them.
"""

import importlib
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.web.server import INDEXABLE_PATHS

ROOT = Path(__file__).resolve().parents[1]

#: A public name for the sitemap to put in its URLs. Set by the fixture, and
#: invented here: a default in the application would be one deployment's domain
#: written into every clone of this repository.
TEST_BASE_URL = "https://public.example"


@pytest.fixture
def web_server(pages_manager, database_with_schema, monkeypatch,
               web_server_factory):
    monkeypatch.setattr("app.web.server.PUBLIC_BASE_URL", TEST_BASE_URL)

    return web_server_factory(pages_manager, database_with_schema)


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

    async def test_robots_lets_the_sitemap_through(self, crawler):
        """`Disallow: /` covers the sitemap unless something says otherwise,
        and Google will not fetch a sitemap robots.txt disallows. It fails with
        an HTTP error against a server answering 200, which is the least
        diagnosable way a sitemap can be broken.
        """
        body = await (await crawler.get("/robots.txt")).text()

        assert "Allow: /sitemap.xml" in body

    async def test_robots_lets_the_preview_image_through(self, crawler):
        """The picture a messenger fetches is its own request, and `Disallow: /`
        would refuse it even though the page that names it is allowed — the
        title renders and the image does not. Same reason as the sitemap."""
        body = await (await crawler.get("/robots.txt")).text()

        assert "Allow: /og-image.png" in body

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
            assert f"<loc>{TEST_BASE_URL}{path}</loc>" in body

    async def test_sitemap_names_no_page_that_needs_a_session(self, crawler):
        body = await (await crawler.get("/sitemap.xml")).text()

        assert "/login" not in body
        assert "/expenses" not in body


class TestReachableFromSomewhere:
    async def test_the_icon_answers_without_a_session(self, crawler):
        """A browser asks for the icon before it knows anything about the site.

        It used to be redirected to the sign-in form, so the tab showed a login
        screen and the request was counted as a visit. Both are wrong for a
        file that carries neither a reader nor a page.
        """
        for path in ("/favicon.ico", "/favicon.svg"):
            response = await crawler.get(path, allow_redirects=False)

            assert response.status == 200, path
            assert response.content_type == "image/svg+xml", path
            assert await response.text()

    async def test_the_icon_is_the_same_at_both_names(self, crawler):
        """A browser picks one name and never asks about the other.

        Serving two different icons would mean the tab depends on which browser
        the reader happened to open.
        """
        ico = await (await crawler.get("/favicon.ico")).read()
        svg = await (await crawler.get("/favicon.svg")).read()

        assert ico == svg

    async def test_the_icon_is_not_a_visit(self, crawler, database_with_schema):
        """It is a file, not a page somebody looked at.

        Counted, it would put a browser's own housekeeping into the log the
        owner reads to find out whether anybody else came.
        """
        from app.db.visits_repo import VisitsRepository

        await crawler.get("/favicon.ico")

        assert VisitsRepository(database_with_schema).recent() == []

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
        """Configured first: with registration closed the page does not exist.

        A deployment with sign-up turned off has nothing to hide from an
        index, and the test would be asserting against a 404.
        """
        monkeypatch.setattr(Config, "SIGNUP_OPEN", True)

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


class TestTheProjectPage:
    """A page of claims about the project, checked against the project.

    The page had gone false twice in ways nothing caught: it said the deploy
    was manual the day after it became automatic, and it counted the tests at
    a number nothing on the planet verifies. A claim about the world is not a
    claim about the code, so a test on the code does not reach it — and the
    failure is invisible until a reader sees it.

    What this covers is the cheap half: names that must exist. It cannot catch
    "this was finished and the page still says it is not", which is why the
    list of what is outstanding now points at the repository instead of being
    copied here.
    """

    @pytest.fixture
    async def page(self, crawler):
        return await (await crawler.get("/project")).text()

    async def test_the_counts_are_the_real_ones(self, page, pages_manager):
        plugins = pages_manager.get_plugins()

        assert f"<b>{len(plugins)}</b><span>плагинов" in page
        assert f"<b>{sum(len(p.commands) for p in plugins)}</b>" \
            "<span>команд" in page

    async def test_the_diagram_lists_exactly_the_shipped_plugins(self, page):
        """The diagram names the plugins by hand, in a picture.

        The counts above come from the manager the page is served with, which in
        a test is not every plugin. This list cannot come from there: the
        diagram claims to show what the project ships, so it is checked against
        what `main.py` registers. Otherwise a seventh plugin would leave the
        diagram quietly showing six and nothing would say so. Both directions
        matter — a plugin renamed in code and not here is the same defect.
        """
        from app.plugins.applications import ApplicationsPlugin
        from app.plugins.clipboard import ClipboardPlugin
        from app.plugins.expenses import ExpensesPlugin
        from app.plugins.notes import NotesPlugin
        from app.plugins.reminders import RemindersPlugin
        from app.plugins.system import SystemPlugin
        from app.plugins.todo import TodoPlugin

        shipped = {
            NotesPlugin.name,
            RemindersPlugin.name,
            ClipboardPlugin.name,
            ExpensesPlugin.name,
            ApplicationsPlugin.name,
            SystemPlugin.name,
            TodoPlugin.name,
        }
        listed = set(re.findall(r'class="cell"[^>]*>(\w+)</text>', page))

        assert listed == shipped

    async def test_the_diagram_carries_a_text_alternative(self, page):
        """A picture with no title is a picture nobody can read with a reader.

        The boxes carry meaning the prose only partly repeats, so the SVG needs a
        name of its own rather than being left anonymous.
        """
        assert 'role="img"' in page
        assert '<title id="diagram-title">' in page

    async def test_every_command_it_names_is_a_command(self, page, every_plugin):
        """The page names commands in prose; every one of them has to be a
        command the platform answers."""
        known = set()

        for plugin in every_plugin.get_plugins():
            known.update(plugin.commands)

        named = set(re.findall(r"<code>(/[a-z]+)</code>", page))

        assert named, "the page names no commands, so this proves nothing"
        assert named <= known, f"named but not real: {sorted(named - known)}"

    async def test_every_file_it_names_is_there(self, page):
        """The page tells a reader which documents to read. A name that is not
        in the repository sends them looking for something that is not there."""

        for name in re.findall(r"<code>([A-Z_]+\.md|[a-z_]+\.py)</code>", page):
            exists = (
                (ROOT / "docs" / name).exists()
                or (ROOT / name).exists()
                or bool(list(ROOT.rglob(name)))
            )
            assert exists, f"{name} is named on the page and does not exist"

    async def test_its_links_go_somewhere_real(self, page, web_server):
        """Every internal address on the page has to be one the application
        serves. A link to a page that was renamed is a 404 a reader finds
        before finding the project.

        Read out of the route table rather than a list written beside the
        test: the first version of this had one, and it failed on a link the
        *menu* had contributed — which is the menu doing its job. The obvious
        fix would have been to shorten the list until the test passed.
        """
        served = {"/", "/login"}

        for resource in web_server.build_app().router.resources():
            canonical = getattr(resource, "canonical", None)
            if canonical:
                served.add(canonical)

        for href in re.findall(r'href="(/[^"]*)"', page):
            # `/demo/<page>` is the demonstration's own sub-application, one
            # route per page and reached through the prefix.
            assert href in served or href.startswith("/demo/"), href

    async def test_it_no_longer_states_a_number_it_cannot_check(self, page):
        """The test count was the worst of them: it understated by a hundred
        and nothing would ever have noticed. The three numbers up there now
        come from the code, so they cannot drift."""
        assert "тестов</span>" not in page


PUBLIC_PAGES = ("/about", "/project")


class TestWhenTheLinkIsShared:
    """A résumé link is passed on in a chat, not opened from a search.

    Without these tags the address arrives as a bare string: no title, no line
    saying what the page is, nothing to look at before the reader decides
    whether to spend a second on it. It is the cheapest thing on the site and
    the only one on the path a recruiter actually walks.
    """

    async def _page(self, client, path: str) -> str:
        return await (await client.get(path)).text()

    @pytest.mark.parametrize("path", PUBLIC_PAGES)
    async def test_it_declares_what_it_is(self, crawler, path):
        body = await self._page(crawler, path)

        assert 'property="og:type"' in body
        assert 'property="og:site_name"' in body
        assert 'property="og:title"' in body
        assert 'property="og:description"' in body
        assert 'name="twitter:card" content="summary_large_image"' in body

    @pytest.mark.parametrize("path", PUBLIC_PAGES)
    async def test_the_tags_say_what_the_page_says(self, crawler, path):
        """Read from the page rather than written beside it.

        A second copy of the title is a second thing to keep right, and the
        copy that goes wrong is not on the page — nobody reading the site
        would ever see it. That is the same failure the project page had with
        its counts.
        """
        body = await self._page(crawler, path)

        title = re.search(r"<title>(.*?)</title>", body, re.S).group(1)
        description = re.search(
            r'name="description" content="(.*?)"', body, re.S
        ).group(1)

        assert f'property="og:title" content="{title}"' in body
        assert f'property="og:description" content="{description}"' in body

    @pytest.mark.parametrize("path", PUBLIC_PAGES)
    async def test_the_address_is_absolute(self, crawler, path):
        """A relative address means nothing to whoever fetches it."""
        body = await self._page(crawler, path)

        assert f'property="og:url" content="{TEST_BASE_URL}{path}"' in body

    @pytest.mark.parametrize("path", PUBLIC_PAGES)
    async def test_the_image_is_absolute_and_sized(self, crawler, path):
        """The messenger needs an absolute address and the true dimensions,
        without fetching the file first."""
        body = await self._page(crawler, path)

        assert f'property="og:image" content="{TEST_BASE_URL}/og-image.png"' in body
        assert 'property="og:image:width" content="1200"' in body
        assert 'property="og:image:height" content="630"' in body

    async def test_the_image_answers_without_a_session(self, crawler):
        """A messenger fetching the preview has no session, the way a browser
        asking for the favicon has none."""
        response = await crawler.get("/og-image.png", allow_redirects=False)

        assert response.status == 200
        assert response.content_type == "image/png"
        assert await response.read()

    async def test_the_about_hero_script_answers_without_a_session(self, crawler):
        """The public page loads it, so a stranger has to be able to fetch it."""
        response = await crawler.get("/about-hero.js", allow_redirects=False)

        assert response.status == 200
        assert response.content_type == "text/javascript"

        page = await (await crawler.get("/about")).text()
        assert 'src="/about-hero.js"' in page

    async def test_no_url_when_the_public_name_is_unknown(
        self, web_server, monkeypatch
    ):
        """`PUBLIC_BASE_URL` is unset on a fresh clone. A url built from
        nothing would name a host this deployment has never heard of — which
        is why the setting has no default either."""
        monkeypatch.setattr("app.web.server.PUBLIC_BASE_URL", "")

        async with TestClient(TestServer(web_server.build_app())) as client:
            body = await self._page(client, "/about")

        assert 'property="og:title"' in body
        assert 'property="og:url"' not in body
        assert 'property="og:image"' not in body

    async def test_the_card_calls_the_site_something(self, crawler):
        """Without a site name the messenger prints the bare address, which is
        the one line of the preview nobody reads."""
        body = await self._page(crawler, "/about")

        assert 'property="og:site_name" content="Платформа автоматизации"' in body

    async def test_the_github_link_is_the_repository(self, crawler):
        """It pointed at the profile — a list of repositories, which is a
        worse thing to send a reader to than the thing itself."""
        for path in PUBLIC_PAGES:
            body = await self._page(crawler, path)

            assert "github.com/Dorian101\"" not in body
            assert "github.com/Dorian101/-automation-platform" in body

    async def test_the_about_page_offers_the_project(self, crawler):
        """The page a reader lands on from a résumé had no way to the one that
        explains the work — only the menu, which you have to already be
        looking for."""
        body = await self._page(crawler, "/about")

        assert 'href="/project"' in body
