import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.web.auth import AUTH_HEADER
from app.web.server import WebServer

TOKEN = "s3cret-token-value"


@pytest.fixture
def secured_server(manager, database_with_schema, monkeypatch):
    monkeypatch.setattr(Config, "WEB_ACCESS_TOKEN", TOKEN)

    return WebServer(manager, database_with_schema, "127.0.0.1", 0)


@pytest.fixture
async def secured_client(secured_server):
    async with TestClient(TestServer(secured_server.build_app())) as test_client:
        yield test_client


class TestAccessControl:
    async def test_rejects_missing_header(self, secured_client):
        response = await secured_client.post(
            "/api/command",
            json={"command": "/notes", "args": ""},
        )

        assert response.status == 401

    async def test_rejects_wrong_header(self, secured_client):
        response = await secured_client.post(
            "/api/command",
            json={"command": "/notes", "args": ""},
            headers={AUTH_HEADER: "wrong"},
        )

        assert response.status == 401

    async def test_rejects_empty_header(self, secured_client):
        response = await secured_client.get(
            "/api/commands",
            headers={AUTH_HEADER: ""},
        )

        assert response.status == 401

    async def test_allows_valid_header(self, secured_client):
        response = await secured_client.post(
            "/api/command",
            json={"command": "/add", "args": "note"},
            headers={AUTH_HEADER: TOKEN},
        )

        assert response.status == 200
        assert (await response.json())["result"] == "Saved"

    async def test_protects_every_route(self, secured_client):
        for path in ["/", "/api/commands", "/health"]:
            response = await secured_client.get(path)
            assert response.status == 401, f"{path} was reachable without auth"

    async def test_health_is_protected(self, secured_client):
        response = await secured_client.get("/health", headers={AUTH_HEADER: TOKEN})

        assert response.status == 200

    async def test_disabled_when_token_unset(
        self,
        manager,
        database_with_schema,
        monkeypatch,
    ):
        monkeypatch.setattr(Config, "WEB_ACCESS_TOKEN", "")

        server = WebServer(manager, database_with_schema, "127.0.0.1", 0)

        async with TestClient(TestServer(server.build_app())) as client:
            response = await client.post(
                "/api/command",
                json={"command": "/notes", "args": ""},
            )

        assert response.status == 200

    async def test_command_is_not_executed_when_denied(
        self,
        secured_client,
        database_with_schema,
    ):
        await secured_client.post(
            "/api/command",
            json={"command": "/add", "args": "must not persist"},
        )

        from app.core.identity import WEB, Identity
        from app.db.notes_repo import NotesRepository

        repo = NotesRepository(database_with_schema)

        assert repo.list(Identity(kind=WEB, id="default")) == []
