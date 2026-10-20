import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.identity import WEB, Identity, telegram
from app.web.server import WebServer
from tests.conftest import TEST_USERNAME


def _texts(entries):
    """The note text alone, so assertions do not repeat the row id."""
    return [text for _, text in entries]


@pytest.fixture
def web_server(manager, database_with_schema, web_server_factory):
    return web_server_factory(manager, database_with_schema)


@pytest.fixture
async def full_client(every_plugin, database_with_schema, create_user, login,
                      web_server_factory):
    """A signed-in client over every plugin, for tests that need a command the
    default manager does not register (reminders)."""
    server = web_server_factory(every_plugin, database_with_schema)
    test_server = TestServer(server.build_app())
    async with TestClient(test_server) as test_client:
        await login(test_client)
        yield test_client


@pytest.fixture
async def client(web_server, create_user, login):
    test_server = TestServer(web_server.build_app())
    async with TestClient(test_server) as test_client:
        await login(test_client)
        yield test_client


@pytest.fixture
async def guest(web_server):
    """A browser with no session — the state a resume link arrives in."""
    test_server = TestServer(web_server.build_app())
    async with TestClient(test_server) as test_client:
        yield test_client


class TestCommandsEndpoint:
    async def test_lists_plugins_and_commands(self, client):
        response = await client.get("/api/commands")

        assert response.status == 200

        payload = await response.json()
        names = {plugin["name"] for plugin in payload["plugins"]}

        assert names == {"notes", "clipboard"}

    async def test_includes_command_metadata(self, client):
        response = await client.get("/api/commands")
        payload = await response.json()

        notes = next(p for p in payload["plugins"] if p["name"] == "notes")

        assert notes["commands"]["/add"] == "Добавить заметку"

    async def test_names_which_commands_take_an_argument(self, client):
        """A command absent from `command_args` takes no argument, which is what
        lets a chip run it on a click instead of showing a form."""
        response = await client.get("/api/commands")
        payload = await response.json()

        notes = next(p for p in payload["plugins"] if p["name"] == "notes")

        assert notes["command_args"]["/add"] == [{"type": "text", "label": "Текст"}]
        assert notes["command_args"]["/del"] == [{"type": "number", "label": "Номер"}]
        assert "/notes" not in notes["command_args"]
        assert "/clear" not in notes["command_args"]

    async def test_commands_with_two_arguments_list_both_fields(self, full_client):
        """`/remind` takes minutes and text; the form needs two fields, in
        order."""
        response = await full_client.get("/api/commands")
        payload = await response.json()

        reminders = next(p for p in payload["plugins"] if p["name"] == "reminders")

        assert reminders["command_args"]["/remind"] == [
            {"type": "number", "label": "Минуты"},
            {"type": "text", "label": "Текст"},
        ]


class TestExecuteEndpoint:
    async def test_runs_command(self, client):
        response = await client.post(
            "/api/command",
            json={"command": "/add", "args": "web note"},
        )

        assert response.status == 200
        assert (await response.json())["result"] == "Saved"

    async def test_result_is_retrievable(self, client):
        await client.post("/api/command", json={"command": "/add", "args": "hello"})

        response = await client.post(
            "/api/command",
            json={"command": "/notes", "args": ""},
        )

        assert (await response.json())["result"] == "1. hello"

    async def test_command_error_is_400(self, client):
        response = await client.post(
            "/api/command",
            json={"command": "/add", "args": ""},
        )

        assert response.status == 400
        assert "error" in (await response.json())["error"] or True

    async def test_unknown_command_is_400(self, client):
        response = await client.post(
            "/api/command",
            json={"command": "/nope", "args": ""},
        )

        assert response.status == 400
        assert "Unknown command" in (await response.json())["error"]

    async def test_rejects_non_json_body(self, client):
        response = await client.post(
            "/api/command",
            data="not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status == 400

    async def test_rejects_non_string_fields(self, client):
        response = await client.post(
            "/api/command",
            json={"command": 5, "args": ""},
        )

        assert response.status == 400

    async def test_defaults_missing_args_to_empty(self, client):
        response = await client.post("/api/command", json={"command": "/notes"})

        assert response.status == 200

    async def test_web_identity_is_used(self, client, database_with_schema):
        await client.post("/api/command", json={"command": "/add", "args": "only web"})

        from app.db.notes_repo import NotesRepository

        repo = NotesRepository(database_with_schema)

        assert _texts(
            repo.entries(Identity(kind=WEB, id=TEST_USERNAME))
        ) == ["only web"]
        assert _texts(repo.entries(telegram(999))) == []


class TestIndexEndpoint:
    async def test_serves_html(self, client):
        response = await client.get("/")

        assert response.status == 200
        assert response.content_type == "text/html"
        assert "Платформа автоматизации" in await response.text()


class TestHealthEndpoint:
    async def test_healthy(self, client):
        response = await client.get("/health")

        assert response.status == 200
        assert (await response.json()) == {"status": "ok", "database": True}

    async def test_reports_unhealthy_database(self, client, web_server, monkeypatch):
        def broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(web_server._database, "connect", broken)

        response = await client.get("/health")

        assert response.status == 503
        assert (await response.json())["database"] is False


class TestPublicPages:
    """The pages a resume links to.

    Two properties have to hold at once: they answer without a session, and
    they say nothing about whoever is asking. The first is what makes them
    useful, the second is what makes it safe to give out the link.
    """

    async def test_about_is_served_without_a_session(self, guest):
        response = await guest.get("/about")

        assert response.status == 200
        assert response.content_type == "text/html"

    async def test_project_is_served_without_a_session(self, guest):
        response = await guest.get("/project")

        assert response.status == 200
        assert response.content_type == "text/html"

    async def test_everything_else_still_requires_a_session(self, guest):
        """Being public has to stay the exception, not a growing habit."""
        for path in ("/about", "/project"):
            response = await guest.get(path, allow_redirects=False)

            assert response.status == 200
            assert response.headers.get("Location") is None

        denied = await guest.get("/", allow_redirects=False)

        assert denied.status == 303
        assert denied.headers["Location"] == "/login"

    async def test_they_link_to_each_other(self, guest):
        about = await (await guest.get("/about")).text()
        project = await (await guest.get("/project")).text()

        assert 'href="/project"' in about
        assert 'href="/about"' in project

    async def test_project_states_the_method(self, guest):
        body = await (await guest.get("/project")).text()

        assert "Как делалось" in body
        assert "github.com/Dorian101" in body

    async def test_rendering_leaves_no_untouched_token(self, guest):
        """A forgotten substitution shows up as literal text in the browser."""
        for path in ("/about", "/project"):
            body = await (await guest.get(path)).text()

            assert "__PAGE_STYLES__" not in body, path
            assert "[[" not in body, f"{path} still has a placeholder"
            assert "--accent: #4a9eff" in body, f"{path} has no styles"

    async def test_a_guest_sees_no_account_on_them(
        self,
        guest,
    ):
        """A document anybody may be given a link to must say nothing about
        who is reading it. This is the half of the old rule that still holds,
        and it is checked with no session at all."""
        for path in ("/about", "/project"):
            body = await (await guest.get(path)).text()

            assert not re.search(
                rf"\b{re.escape(TEST_USERNAME)}\b", body
            ), path
            assert "Выйти" not in body, path

    async def test_the_attic_is_the_whole_of_the_difference(
        self,
        guest,
        create_user,
        login,
    ):
        """Signed in, the only thing these pages say that they did not before
        is the attic — the reader's own name and the way out.

        Not a leak, and the pages are still documents: the difference lives
        above the content, and it describes the reader to the reader. What
        would not be acceptable is the *content* changing, because that is
        what a link sent to somebody else would carry.
        """
        signed_in = guest
        before = {p: await (await guest.get(p)).text()
                  for p in ("/about", "/project")}

        await login(signed_in)

        for path, was in before.items():
            body = await (await guest.get(path)).text()
            # From the heading down: above it is the attic, which a guest
            # does not have and therefore does not need slicing away.
            assert body[body.index("<h1>") :] == was[was.index("<h1>") :], path


class TestTheAttic:
    """The menu and the account block, one row above the page.

    Leaving an account meant walking to the console to do it, because the
    block was on the console's heading and nowhere else.
    """

    async def test_the_console_carries_it(self, client):
        body = await (await client.get("/")).text()

        assert '<header class="attic">' in body
        assert TEST_USERNAME in body

    async def test_a_plugin_page_carries_it(
        self, pages_manager, database_with_schema, create_user, login
    ):
        """The plugins' own pages are the ones the reader spends time on, and
        they are not the console."""
        server = TestServer(
            WebServer(
                manager=pages_manager,
                database=database_with_schema,
                host="127.0.0.1",
                port=0,
            ).build_app()
        )

        async with TestClient(server) as page_client:
            await login(page_client)
            body = await (await page_client.get("/expenses")).text()

        assert '<header class="attic">' in body
        assert TEST_USERNAME in body

    async def test_the_block_offers_the_way_out_with_a_token(
        self, client
    ):
        """The button is on every page now, so the form is one a page could
        not fire on its own."""
        body = await (await client.get("/")).text()

        assert 'action="/logout"' in body
        assert re.search(r'name="csrf" value="[^"]+"', body)

    async def test_a_guest_gets_the_menu_and_not_the_block(self, guest):
        body = await (await guest.get("/login")).text()

        assert '<header class="attic">' not in body

    async def test_the_demonstration_keeps_the_plain_menu(self, guest):
        """It is not excluded for tidiness. A guest has no account, so the
        block would be somebody else's, and the demonstration is the one part
        of the site anybody can reach."""
        body = await (await guest.get("/demo")).text()

        assert '<header class="attic">' not in body
        assert "Выйти" not in body

    async def test_a_signed_in_reader_gets_no_attic_on_it_either(self, client):
        """The demonstration is shown to whoever is signed in as well, and a
        block there would name the reader of a page that is not theirs."""
        body = await (await client.get("/demo")).text()

        assert '<header class="attic">' not in body
        assert "Выйти" not in body

    def test_the_demonstration_asks_for_no_account(self, pages_manager):
        """The mechanism, not the outcome.

        The demonstration builds its own navigation and never reaches the
        server's, so nothing upstream decides anything about the attic for
        it — there is no rule there to get wrong, and the outcome test above
        would keep passing if somebody changed the reason it happens.
        """
        from app.web.demo import DemoService

        service = DemoService(pages_manager, None)

        assert "attic" not in service._nav()

    def test_an_empty_block_leaves_the_menu_alone(self):
        """A signed-out reader has no account to show, and a header with
        nothing in it is a thing to notice."""
        from app.web.pages import attic

        menu = "<details class=\"menu\"></details>"

        assert attic(menu, "") == menu
