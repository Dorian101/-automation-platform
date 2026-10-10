from datetime import date
from decimal import Decimal

import pytest

from app.core.identity import WEB, Identity
from app.core.results import CommandError
from app.plugins.expenses import ExpensesPlugin

OTHER = Identity(kind=WEB, id="someone-else")
THIS_MONTH = date.today().strftime("%Y-%m")


@pytest.fixture
def plugin(database_with_schema):
    return ExpensesPlugin(database_with_schema)


@pytest.fixture
def food(plugin, web_identity):
    return plugin.repo.add_category(web_identity, "Еда")


class TestPageDeclaration:
    def test_declares_a_page_and_is_web_only(self, plugin):
        assert plugin.page == "/expenses"
        assert plugin.web_only is True


class TestMonthHelpers:
    def test_labels_a_month(self):
        from app.plugins.expenses import _month_label

        assert _month_label("2026-05") == "май 2026"
        assert _month_label("2026-01") == "январь 2026"

    def test_an_out_of_range_month_is_not_mislabelled(self):
        """A name built from the number would read 'май' for month 13."""
        from app.plugins.expenses import _month_label

        assert _month_label("2026-13") == "2026-13"

    def test_shifts_backwards_across_a_year_boundary(self):
        from app.plugins.expenses import _shift_month

        assert _shift_month("2026-01", -1) == "2025-12"

    def test_shifts_forwards_across_a_year_boundary(self):
        from app.plugins.expenses import _shift_month

        assert _shift_month("2025-12", 1) == "2026-01"

    def test_shifts_within_a_year(self):
        from app.plugins.expenses import _shift_month

        assert _shift_month("2026-05", -1) == "2026-04"
        assert _shift_month("2026-05", 1) == "2026-06"


class TestView:
    async def test_empty_month_reports_nothing_rather_than_failing(
        self, plugin, web_identity
    ):
        view = await plugin.page_view(web_identity)

        assert view["chart"]["items"] == []
        assert view["totals"]["current"] == "0"
        assert view["totals"]["has_previous"] is False

    async def test_chart_orders_and_shares_match_the_table(
        self, plugin, web_identity, food
    ):
        rent = plugin.repo.add_category(web_identity, "Аренда")
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 5, 1))
        plugin.repo.add(web_identity, rent, Decimal("300"), date(2026, 5, 1))

        view = await plugin.page_action(
            web_identity, "month", {"month": "2026-05"}
        )

        labels = [item["label"] for item in view["chart"]["items"]]

        assert labels == ["Аренда", "Еда"]

        # The chart and the table come from one aggregation, so the largest
        # bar is always the largest total and the shares always add up.
        assert view["chart"]["items"][0]["share"] == 100.0
        assert view["totals"]["current"] == "400"
        assert sum(
            float(item["share_text"].rstrip("%"))
            for item in view["chart"]["items"]
        ) == pytest.approx(100, abs=1)

    async def test_change_is_reported_only_against_a_previous_month(
        self, plugin, web_identity, food
    ):
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 4, 10))
        plugin.repo.add(web_identity, food, Decimal("150"), date(2026, 5, 10))

        view = await plugin.page_action(web_identity, "month", {"month": "2026-05"})

        assert view["totals"]["previous"] == "100"
        assert "50.0%" in view["totals"]["change"]

    async def test_first_month_has_no_change_figure(
        self, plugin, web_identity, food
    ):
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 5, 10))

        view = await plugin.page_action(web_identity, "month", {"month": "2026-05"})

        assert view["totals"]["change"] == ""

    async def test_another_accounts_spending_is_absent(
        self, plugin, web_identity, food
    ):
        plugin.repo.add(web_identity, food, Decimal("500"), date(2026, 5, 1))

        view = await plugin.page_action(OTHER, "month", {"month": "2026-05"})

        assert view["totals"]["current"] == "0"
        assert view["chart"]["items"] == []


class TestSpendAction:
    async def test_records_an_expense(self, plugin, web_identity, food):
        await plugin.page_action(
            web_identity,
            "spend",
            {"category_id": str(food), "amount": "320", "spent_at": "2026-05-03"},
        )

        entries = plugin.repo.recent(web_identity)

        assert entries[0]["amount"] == Decimal("320")

    async def test_accepts_a_comma_as_the_decimal_point(
        self, plugin, web_identity, food
    ):
        await plugin.page_action(
            web_identity,
            "spend",
            {"category_id": str(food), "amount": "12,50", "spent_at": "2026-05-03"},
        )

        assert plugin.repo.recent(web_identity)[0]["amount"] == Decimal("12.50")

    async def test_rejects_a_non_numeric_amount(
        self, plugin, web_identity, food
    ):
        with pytest.raises(CommandError):
            await plugin.page_action(
                web_identity,
                "spend",
                {"category_id": str(food), "amount": "abc"},
            )

    async def test_rejects_a_zero_or_negative_amount(
        self, plugin, web_identity, food
    ):
        for amount in ("0", "-5"):
            with pytest.raises(CommandError):
                await plugin.page_action(
                    web_identity,
                    "spend",
                    {"category_id": str(food), "amount": amount},
                )

    async def test_rejects_a_bad_date(self, plugin, web_identity, food):
        with pytest.raises(CommandError):
            await plugin.page_action(
                web_identity,
                "spend",
                {
                    "category_id": str(food),
                    "amount": "10",
                    "spent_at": "03.05.2026",
                },
            )

    async def test_rejects_a_foreign_category(self, plugin, web_identity):
        theirs = plugin.repo.add_category(OTHER, "Ипотека")

        with pytest.raises(CommandError):
            await plugin.page_action(
                web_identity,
                "spend",
                {"category_id": str(theirs), "amount": "50"},
            )


class TestCategoryActions:
    async def test_adds_a_category(self, plugin, web_identity):
        await plugin.page_action(web_identity, "catadd", {"name": "Транспорт"})

        assert [c["name"] for c in plugin.repo.categories(web_identity)] == [
            "Транспорт"
        ]

    async def test_rejects_an_empty_name(self, plugin, web_identity):
        with pytest.raises(CommandError):
            await plugin.page_action(web_identity, "catadd", {"name": "   "})

    async def test_rejects_an_overlong_name(self, plugin, web_identity):
        with pytest.raises(CommandError):
            await plugin.page_action(web_identity, "catadd", {"name": "я" * 41})

    async def test_duplicate_says_so_in_plain_words(
        self, plugin, web_identity
    ):
        """The index is the rule, not the plugin; its own message names a
        constraint nobody can act on."""
        plugin.repo.add_category(web_identity, "Еда")

        with pytest.raises(CommandError) as error:
            await plugin.page_action(web_identity, "catadd", {"name": "Еда"})

        assert "уже есть" in str(error.value)

    async def test_categories_are_per_account(self, plugin, web_identity):
        plugin.repo.add_category(web_identity, "Еда")

        view = await plugin.page_action(OTHER, "month", {})

        options = view["forms"][0]["fields"][0]["options"]

        assert options == []


class TestDeleteAction:
    async def test_deletes_an_expense(self, plugin, web_identity, food):
        await plugin.page_action(
            web_identity,
            "spend",
            {"category_id": str(food), "amount": "10", "spent_at": "2026-05-01"},
        )
        expense_id = plugin.repo.recent(web_identity)[0]["id"]

        await plugin.page_action(
            web_identity, "delete", {"expense_id": str(expense_id)}
        )

        assert plugin.repo.recent(web_identity) == []

    async def test_cannot_delete_another_accounts_expense(
        self, plugin, web_identity, food
    ):
        await plugin.page_action(
            web_identity,
            "spend",
            {"category_id": str(food), "amount": "10", "spent_at": "2026-05-01"},
        )
        expense_id = plugin.repo.recent(web_identity)[0]["id"]

        await plugin.page_action(
            OTHER, "delete", {"expense_id": str(expense_id)}
        )

        assert plugin.repo.recent(web_identity) != []


class TestWidenedRead:
    async def test_spending_shows_across_a_linked_pair(self, plugin):
        web = Identity(kind=WEB, id="alexey")
        chat = Identity(kind="telegram", id="555")

        class Resolver:
            def identities(self, identity):
                return (web, chat)

        linked = ExpensesPlugin(plugin.repo.database, persons=Resolver())
        food = linked.repo.add_category(web, "Еда")
        linked.repo.add(web, food, Decimal("77"), date(2026, 5, 1))

        view = await linked.page_action(web, "month", {"month": "2026-05"})

        assert view["totals"]["current"] == "77"
