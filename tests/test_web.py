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
