from datetime import date

import pytest

from app.core.identity import guest, is_guest
from app.plugins.applications import ApplicationsPlugin
from app.plugins.expenses import ExpensesPlugin


@pytest.fixture
def visitor():
    return guest("a-token")


class TestGuestIdentity:
    def test_namespaces_the_visitor(self, visitor):
        assert str(visitor) == "web:guest:a-token"

    def test_a_guest_is_recognised(self, visitor):
        assert is_guest(visitor) is True

    def test_an_account_is_not_a_guest(self, web_identity):
        assert is_guest(web_identity) is False

    def test_the_round_trip_is_stable(self, visitor):
        """The prefix carries a colon of its own; parsing must survive it."""
        from app.core.identity import Identity

        assert Identity.parse(str(visitor)) == visitor


@pytest.fixture
def expenses(database_with_schema):
    return ExpensesPlugin(database_with_schema)


@pytest.fixture
def applications(database_with_schema):
    return ApplicationsPlugin(database_with_schema)


class TestSpendingSeed:
    async def test_fills_an_empty_namespace(self, expenses, visitor):
        await expenses.seed_demo(visitor)

        view = await expenses.page_view(visitor)

        assert view["totals"]["items"][0]["value"] != "0"
        assert view["chart"]["items"]

    async def test_covers_more_than_one_month(self, expenses, visitor):
        """A single month would make the arrows and the comparison look broken."""
        await expenses.seed_demo(visitor)

        view = await expenses.page_view(visitor)

        assert view["totals"]["items"][1]["caption"] == "За прошлый месяц"
        assert view["totals"]["delta"] is not None

    async def test_nothing_is_dated_in_the_future(self, expenses, visitor):
        """An expense dated ahead would count days that have not happened."""
        await expenses.seed_demo(visitor)

        for row in (await expenses.page_view(visitor))["list"]["rows"]:
            assert date.fromisoformat(row["spent_at"]) <= date.today()

    async def test_the_amounts_are_not_all_round(self, expenses, visitor):
        """A demo made of round numbers gives away that nobody used it."""
        await expenses.seed_demo(visitor)

        totals = (await expenses.page_view(visitor))["totals"]["items"]

        assert totals[0]["value"].strip() != "0"


class TestApplicationsSeed:
    async def test_fills_an_empty_namespace(self, applications, visitor):
        await applications.seed_demo(visitor)

        view = await applications.page_view(visitor)

        assert view["table"]["rows"]

    async def test_spreads_across_the_pipeline(
        self, applications, visitor
    ):
        """Everything still waiting would say nothing about what the funnel
        does; everything at an offer would say nothing about what usually
        happens."""
        await applications.seed_demo(visitor)

        view = await applications.page_view(visitor)
        stages = [row["status"] for row in view["table"]["rows"]]

        assert "applied" in stages
        assert "answered" in stages
        assert "offer" in stages
        assert "rejected" in stages

    async def test_every_seeded_stage_was_reachable(
        self, applications, visitor
    ):
        """The seed is not exempt from the machine it demonstrates.

        `interview` reached from `applied` in one step is exactly the move
        `_set_stage` refuses, so a seed that assigned stages directly would be
        showing a state the interface cannot produce.
        """
        await applications.seed_demo(visitor)

        view = await applications.page_view(visitor)

        for row in view["table"]["rows"]:
            assert row["status"] in (
                "applied",
                "answered",
                "interview",
                "offer",
                "rejected",
            )

    async def test_nothing_is_dated_in_the_future(self, applications, visitor):
        await applications.seed_demo(visitor)

        for row in (await applications.page_view(visitor))["table"]["rows"]:
            assert date.fromisoformat(row["applied_at"]) <= date.today()

    async def test_some_rows_are_silent(self, applications, visitor):
        """The silence column exists to be demonstrated; a seed that answered
        everything within five days would leave it empty for every row."""
        await applications.seed_demo(visitor)

        view = await applications.page_view(visitor)

        assert any(row["silent"] for row in view["table"]["rows"])
