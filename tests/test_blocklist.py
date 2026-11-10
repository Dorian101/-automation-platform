import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.web import ratelimit
from app.web.auth import AUTH_HEADER
from app.web.ratelimit import Blocklist

HEADERS = {AUTH_HEADER: Config.WEB_ACCESS_TOKEN} if Config.WEB_ACCESS_TOKEN else {}


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    return now


class TestBlocklist:
    def test_three_strikes_an_hour_block_for_a_day(self, clock):
        blocklist = Blocklist()

        assert blocklist.strike("1.2.3.4") is False
        clock[0] += 61
        assert blocklist.strike("1.2.3.4") is False
        clock[0] += 61
        assert blocklist.strike("1.2.3.4") is True

        assert blocklist.blocked("1.2.3.4")
        assert not blocklist.blocked("5.6.7.8")
        clock[0] += 86400
        assert not blocklist.blocked("1.2.3.4")

    def test_a_burst_of_refusals_is_one_strike(self, clock):
        blocklist = Blocklist()

        for _ in range(10):
            blocklist.strike("1.2.3.4")

        assert not blocklist.blocked("1.2.3.4")

    def test_strikes_older_than_an_hour_are_forgotten(self, clock):
        blocklist = Blocklist()

        blocklist.strike("1.2.3.4")
        clock[0] += 1800
        blocklist.strike("1.2.3.4")
        clock[0] += 1900

        assert blocklist.strike("1.2.3.4") is False
        assert not blocklist.blocked("1.2.3.4")


@pytest.fixture
async def client(manager, database_with_schema, web_server_factory):
    server = web_server_factory(manager, database_with_schema)
    async with TestClient(TestServer(server.build_app())) as made:
        yield made


async def _feedback(client):
    return await client.post(
        "/feedback", json={"text": "Хороший сайт", "page": "/about"}, headers=HEADERS
    )


class TestTheSite:
    async def test_an_address_that_keeps_hitting_limits_is_shut_out(
        self, client, clock
    ):
        for _ in range(3):
            assert (await _feedback(client)).status == 200

        for _ in range(3):
            assert (await _feedback(client)).status == 429
            clock[0] += 61

        assert (await client.get("/about", headers=HEADERS)).status == 403

    async def test_the_owners_address_is_never_shut_out(
        self, client, clock, monkeypatch
    ):
        monkeypatch.setattr(Config, "VISIT_IGNORE_IPS", {"127.0.0.1"})

        for _ in range(3):
            await _feedback(client)
        for _ in range(3):
            await _feedback(client)
            clock[0] += 61

        assert (await client.get("/about", headers=HEADERS)).status == 200

    async def test_too_many_open_tabs_is_not_abuse(
        self, client, clock, monkeypatch, login, create_user
    ):
        monkeypatch.setattr("app.web.live.MAX_PER_PERSON", 0)
        await login(client)

        for _ in range(3):
            assert (await client.get("/api/events", headers=HEADERS)).status == 429
            clock[0] += 61

        assert (await client.get("/about", headers=HEADERS)).status == 200
