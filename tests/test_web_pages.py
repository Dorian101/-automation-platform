import json
import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.web.server import WebServer


@pytest.fixture
def web_server(expenses_manager, database_with_schema):
    return WebServer(
        manager=expenses_manager,
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
    test_server = TestServer(web_server.build_app())
    async with TestClient(test_server) as test_client:
        yield test_client


def _view(body: str) -> dict:
    """The description the page embedded for the browser to paint."""
    match = re.search(
        r'<script type="application/json" id="initial-view">(.*?)</script>',
        body,
        re.S,
    )
    assert match, "page did not embed its description"
    return json.loads(match.group(1))


class TestPageRoute:
    async def test_page_is_served_to_a_signed_in_browser(self, client):
        response = await client.get("/expenses")

        assert response.status == 200
        assert "text/html" in response.headers["Content-Type"]

    async def test_page_embeds_its_description(self, client):
        body = await (await client.get("/expenses")).text()

        view = _view(body)

        assert view["title"] == "Расходы"
        assert "chart" in view
        assert "forms" in view

    async def test_page_is_protected_from_a_guest(self, guest):
        """No session means no page. The auth middleware answers before the
        handler runs, so the browser gets the login form rather than the
        analytics."""
        response = await guest.get("/expenses", allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == "/login"

    async def test_api_call_without_a_session_is_401(self, guest):
        response = await guest.post(
            "/api/page/action",
            json={"page": "/expenses", "action": "month", "payload": {}},
        )

        assert response.status == 401

    async def test_a_path_no_plugin_claimed_is_404(self, client):
        """Nothing routes an unclaimed path, so there is nothing to leak."""
        response = await client.get("/not-a-page")

        assert response.status == 404

    async def test_pages_endpoint_lists_the_owned_pages(self, client):
        payload = await (await client.get("/api/pages")).json()

        pages = {page["page"]: page["name"] for page in payload["pages"]}

        assert pages == {"/expenses": "expenses"}

    async def test_a_page_is_named_for_a_reader_not_by_path(self, client):
        """The title is what a person sees, so it is not the plugin's name."""
        payload = await (await client.get("/api/pages")).json()

        assert payload["pages"][0]["title"] == "Расходы"

    async def test_the_console_links_to_every_page(self, client):
        """Otherwise a page exists and nothing points at it."""
        await client.get("/api/pages")

        body = await (await client.get("/")).text()

        assert "/api/pages" in body

    async def test_the_page_embeds_its_path_not_the_plugin_name(
        self, client
    ):
        """The action endpoint resolves the owning plugin by path.

        Embedding the plugin name here instead made every button on the page
        answer "Unknown page", which is exactly what it did: the page looked
        fine and nothing on it worked.
        """
        body = await (await client.get("/expenses")).text()

        assert "'/expenses'" in body
        assert "'expenses'" not in body


class TestPageAction:
    async def _act(self, client, action, payload):
        return await client.post(
            "/api/page/action",
            json={"page": "/expenses", "action": action, "payload": payload},
        )

    async def test_unknown_page_is_404(self, client):
        response = await client.post(
            "/api/page/action",
            json={"page": "/nothing", "action": "month", "payload": {}},
        )

        assert response.status == 404

    async def test_unknown_action_is_400(self, client):
        response = await self._act(client, "explode", {})

        assert response.status == 400

    async def test_month_changes_the_report(self, client):
        response = await self._act(client, "month", {"month": "2026-05"})

        view = (await response.json())["view"]

        assert view["month"]["value"] == "2026-05"
        assert view["month"]["label"] == "май 2026"

    async def test_adding_a_category_is_reflected_on_the_page(self, client):
        await self._act(client, "catadd", {"name": "Еда"})

        response = await self._act(client, "month", {"month": "2026-05"})
        options = (await response.json())["view"]["forms"][0]["fields"][0]["options"]

        assert [option["label"] for option in options] == ["Еда"]

    async def test_spending_shows_up_in_the_returned_view(self, client):
        await self._act(client, "catadd", {"name": "Еда"})

        response = await self._act(client, "month", {"month": "2026-05"})
        form = (await response.json())["view"]["forms"][0]
        category_id = form["fields"][0]["options"][0]["value"]
        today = form["fields"][2]["value"]

        response = await self._act(
            client,
            "spend",
            {"category_id": category_id, "amount": "320", "spent_at": today},
        )

        assert response.status == 200
        view = (await response.json())["view"]
        assert view["title"] == "Расходы"
        assert view["recent"][0]["amount"] == "320"

    async def test_a_plugin_error_comes_back_as_400(self, client):
        response = await self._act(client, "catadd", {"name": ""})

        assert response.status == 400
        assert "error" in (await response.json())

    async def test_rejects_a_payload_that_is_not_an_object(self, client):
        response = await client.post(
            "/api/page/action",
            json={"page": "/expenses", "action": "month", "payload": "nope"},
        )

        assert response.status == 400

    async def test_a_malformed_month_is_a_400_not_a_500(self, client):
        """The browser is not trusted and the page caused this one.

        It used to answer "Internal Error" for a month it had mangled itself,
        which points a reader at the server when the fault was on the page.
        """
        response = await self._act(client, "month", {"month": "24320-09"})

        assert response.status == 400
        assert "месяц" in (await response.json())["error"]

    async def test_the_view_carries_its_neighbouring_months(self, client):
        body = await (await client.get("/expenses")).text()

        view = _view(body)

        assert view["month"]["prev"] and view["month"]["next"]
        # The page does not recompute these; if it did, the server would have
        # no reason to send them.
        assert view["month"]["prev"] < view["month"]["value"]

    async def test_the_month_control_is_the_two_arrows_only(self, client):
        """A select brings its own up/down arrows.

        With arrows either side of it the control offered two different ways
        to change the month and neither listed every month holding data, so
        the navigation is the two buttons and the label between them.
        """
        body = await (await client.get("/expenses")).text()

        assert '<select id="month"' not in body
        assert 'id="month"' in body and "readonly" in body
        assert "prev-month" in body and "next-month" in body


class TestIsolation:
    """Two accounts on the same page must not see each other's spending."""

    async def test_one_accounts_categories_are_absent_from_the_other(
        self, web_server, create_user, login
    ):
        create_user("first-user")
        create_user("second-user")

        server = TestServer(web_server.build_app())

        async with TestClient(server) as first, TestClient(server) as second:
            await login(first, username="first-user")
            await login(second, username="second-user")

            async def act(client, action, payload):
                response = await client.post(
                    "/api/page/action",
                    json={"page": "/expenses", "action": action, "payload": payload},
                )
                return await response.json()

            await act(first, "catadd", {"name": "Еда"})
            await act(second, "catadd", {"name": "Дом"})

            first_view = (await act(first, "month", {"month": "2026-05"}))["view"]
            second_view = (await act(second, "month", {"month": "2026-05"}))["view"]

        first_options = first_view["forms"][0]["fields"][0]["options"]
        second_options = second_view["forms"][0]["fields"][0]["options"]

        assert [o["label"] for o in first_options] == ["Еда"]
        assert [o["label"] for o in second_options] == ["Дом"]

    async def test_one_accounts_spending_is_absent_from_the_other(
        self, web_server, create_user, login
    ):
        create_user("first-user")
        create_user("second-user")

        server = TestServer(web_server.build_app())

        async with TestClient(server) as first, TestClient(server) as second:
            await login(first, username="first-user")
            await login(second, username="second-user")

            async def act(client, action, payload):
                response = await client.post(
                    "/api/page/action",
                    json={"page": "/expenses", "action": action, "payload": payload},
                )
                return await response.json()

            await act(first, "catadd", {"name": "Еда"})
            form = (await act(first, "month", {"month": "2026-05"}))["view"][
                "forms"
            ][0]

            # The date the page offers is today, so the month under test is
            # read back from it rather than hardcoded — a report for a month
            # that has nothing in it correctly shows zero.
            spent_at = form["fields"][2]["value"]
            month = spent_at[:7]

            await act(
                first,
                "spend",
                {
                    "category_id": form["fields"][0]["options"][0]["value"],
                    "amount": "500",
                    "spent_at": spent_at,
                },
            )

            first_view = (await act(first, "month", {"month": month}))["view"]
            second_view = (await act(second, "month", {"month": month}))["view"]

        assert first_view["totals"]["current"] == "500"
        assert second_view["totals"]["current"] == "0"
        assert second_view["chart"]["items"] == []
