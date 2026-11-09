"""The guest book on the public pages: what it keeps, what it refuses, and
who hears about it."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.identity import WEB
from app.db.feedback_repo import FeedbackRepository
from app.web.server import FEEDBACK_ATTEMPTS, FEEDBACK_MAX_LENGTH
from tests.conftest import TEST_USERNAME


class FakeNotifier:
    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail
        self.hang = False

    async def deliver(self, target, text, **_):
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append((target, text))
        return True


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
async def client(manager, database_with_schema, web_server_factory, notifier):
    server = web_server_factory(manager, database_with_schema, notifier=notifier)
    test_client = TestClient(TestServer(server.build_app()))
    test_client.web_server = server
    await test_client.start_server()
    yield test_client
    for task in server._sending:
        task.cancel()
    await test_client.close()


async def _settled(client):
    """Wait for the background sends the last request started."""
    await asyncio.gather(*client.web_server._sending)


@pytest.fixture
def admin(create_user, users):
    account = users.get_by_username(TEST_USERNAME)
    users.set_admin(account.id, True)
    return account


def _saved(database):
    return FeedbackRepository(database).recent()


class TestFeedback:
    async def test_a_note_is_kept_and_sent_to_the_admin(
        self, client, admin, notifier, database_with_schema
    ):
        response = await client.post(
            "/feedback", json={"text": "  Отличный сайт  ", "page": "/about"}
        )

        assert response.status == 200
        saved = _saved(database_with_schema)
        assert [(f.page, f.text) for f in saved] == [("/about", "Отличный сайт")]
        await _settled(client)
        assert len(notifier.sent) == 1
        target, text = notifier.sent[0]
        assert target.kind == WEB and target.id == TEST_USERNAME
        assert "Отличный сайт" in text and "/about" in text

    async def test_no_session_is_needed(self, client, database_with_schema):
        """A stranger is exactly who the form is for."""
        response = await client.post("/feedback", json={"text": "привет"})

        assert response.status == 200
        assert len(_saved(database_with_schema)) == 1

    async def test_only_administrators_are_told(
        self, client, create_user, notifier
    ):
        await client.post("/feedback", json={"text": "привет"})
        await _settled(client)

        assert notifier.sent == []

    async def test_an_unknown_page_is_not_stored(self, client, database_with_schema):
        await client.post(
            "/feedback", json={"text": "привет", "page": "<script>"}
        )

        assert _saved(database_with_schema)[0].page == ""

    @pytest.mark.parametrize("text", ["", "   ", None, 5])
    async def test_empty_or_wrong_text_is_refused(
        self, client, database_with_schema, text
    ):
        response = await client.post("/feedback", json={"text": text})

        assert response.status == 400
        assert _saved(database_with_schema) == []

    async def test_too_long_text_is_refused(self, client, database_with_schema):
        response = await client.post(
            "/feedback", json={"text": "a" * (FEEDBACK_MAX_LENGTH + 1)}
        )

        assert response.status == 400
        assert _saved(database_with_schema) == []

    async def test_a_form_post_is_refused(self, client, database_with_schema):
        """A plain form from another site cannot send JSON without a preflight."""
        response = await client.post("/feedback", data={"text": "spam"})

        assert response.status == 415
        assert _saved(database_with_schema) == []

    async def test_a_filled_trap_field_is_dropped_quietly(
        self, client, admin, notifier, database_with_schema
    ):
        response = await client.post(
            "/feedback", json={"text": "buy now", "trap": "spam"}
        )

        assert response.status == 200
        assert _saved(database_with_schema) == []
        assert notifier.sent == []

    async def test_one_address_is_limited(self, client):
        for _ in range(FEEDBACK_ATTEMPTS):
            await client.post("/feedback", json={"text": "раз"})

        response = await client.post("/feedback", json={"text": "ещё"})

        assert response.status == 429

    async def test_the_answer_does_not_wait_for_telegram(
        self, client, admin, notifier
    ):
        """A hanging send must not hold the visitor's request open."""
        notifier.hang = True

        response = await asyncio.wait_for(
            client.post("/feedback", json={"text": "привет"}), timeout=5
        )

        assert response.status == 200

    async def test_a_failed_send_still_keeps_the_note(
        self, client, admin, notifier, database_with_schema
    ):
        notifier.fail = True

        response = await client.post("/feedback", json={"text": "привет"})

        assert response.status == 200
        assert len(_saved(database_with_schema)) == 1


class TestTheButton:
    @pytest.mark.parametrize("path", ["/about", "/project", "/demo"])
    async def test_every_public_page_has_it(self, client, path):
        body = await (await client.get(path)).text()

        assert 'class="feedback-open"' in body
        assert '<script src="/feedback.js" defer></script>' in body

    async def test_the_script_is_public(self, client):
        response = await client.get("/feedback.js")

        assert response.status == 200
        assert "/feedback" in await response.text()


class TestTheAdminPage:
    async def test_it_shows_the_notes_escaped(
        self, client, admin, login, database_with_schema
    ):
        FeedbackRepository(database_with_schema).add("<b>жирно</b>", "/project")
        await login(client)

        body = await (await client.get("/admin/feedback")).text()

        assert "&lt;b&gt;жирно&lt;/b&gt;" in body
        assert "/project" in body
        assert "aria-current='page'>Отзывы" in body
