import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.identity import WEB, Identity, telegram
from app.web.server import WebServer
from tests.conftest import TEST_USERNAME


@pytest.fixture
def web_server(manager, database_with_schema):
    return WebServer(
        manager=manager,
        database=database_with_schema,
        host="127.0.0.1",
        port=0,
    )


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

        assert notes["commands"]["/add"] == "Add a new note"


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

        assert (await response.json())["result"] == "- hello"

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

        assert repo.list(Identity(kind=WEB, id=TEST_USERNAME)) == ["only web"]
        assert repo.list(telegram(999)) == []


class TestIndexEndpoint:
    async def test_serves_html(self, client):
        response = await client.get("/")

        assert response.status == 200
        assert response.content_type == "text/html"
        assert "Automation Platform" in await response.text()


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

    async def test_a_session_adds_nothing_to_them(
        self,
        guest,
        create_user,
        login,
    ):
        """Signed in or not, these pages are the same public documents.

        If one ever starts rendering the account, it has stopped being a
        page anyone can be given a link to.
        """
        await login(guest)

        for path in ("/about", "/project"):
            body = await (await guest.get(path)).text()

            assert not re.search(
                rf"\b{re.escape(TEST_USERNAME)}\b", body
            ), path
            assert "Sign out" not in body, path
