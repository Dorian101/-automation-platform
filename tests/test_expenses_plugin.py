from datetime import date
from decimal import Decimal

import pytest

from app.core.identity import WEB, Identity
from app.core.results import CommandError
from app.plugins.expenses import ExpensesPlugin

OTHER = Identity(kind=WEB, id="someone-else")
THIS_MONTH = date.today().strftime("%Y-%m")


def _total(view, index: int) -> str:
    """The value of the nth figure in the totals row.

    Totals are a list of labelled figures rather than named fields, so that
    the page draws whatever a plugin sends instead of knowing what to expect.
    """
    return view["totals"]["items"][index]["value"]


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


class TestNeighbourMonths:
    """The page is handed ready-made neighbours and works out nothing itself.

    It used to compute them in the browser, where the year was multiplied by
    twelve and never divided back — September 2026 came out as "сентябрь
    24320", and every click after that walked further from a real date.
    """

    async def test_neighbours_come_from_the_plugin(
        self, plugin, web_identity
    ):
        view = await plugin.page_action(
            web_identity, "month", {"month": "2026-01"}
        )

        assert view["month"]["prev"] == "2025-12"
        assert view["month"]["next"] == "2026-02"

    async def test_neighbours_cross_a_year_correctly(
        self, plugin, web_identity
    ):
        view = await plugin.page_action(
            web_identity, "month", {"month": "2026-12"}
        )

        assert view["month"]["next"] == "2027-01"

    async def test_a_malformed_month_is_refused_not_crashed(
        self, plugin, web_identity
    ):
        """A month nobody can parse must not reach the query.

        The page used to send "24320-09" after one bad click, which the
        database cannot read as a date — a 500 for something the page caused.
        A blank month is not in this list: blank means "not given", and the
        current month is the right answer to that.
        """
        for bad in ("24320-09", "2026-13", "not-a-month", "2026-1", "2026-00"):
            with pytest.raises(CommandError):
                await plugin.page_action(
                    web_identity, "month", {"month": bad}
                )


class TestView:
    async def test_empty_month_reports_nothing_rather_than_failing(
        self, plugin, web_identity
    ):
        view = await plugin.page_action(web_identity, "month", {"screen": "analytics"})

        assert view["chart"]["items"] == []
        assert _total(view, 0) == "0"
        # No previous month to compare against, so no cell for it and no
        # delta: both would be saying nothing.
        captions = [item["caption"] for item in view["totals"]["items"]]
        assert "За прошлый месяц" not in captions
        assert view["totals"]["delta"] is None

    async def test_chart_orders_and_shares_match_the_table(
        self, plugin, web_identity, food
    ):
        rent = plugin.repo.add_category(web_identity, "Аренда")
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 5, 1))
        plugin.repo.add(web_identity, rent, Decimal("300"), date(2026, 5, 1))

        view = await plugin.page_action(
            web_identity, "month", {"month": "2026-05", "screen": "analytics"}
        )

        labels = [item["label"] for item in view["chart"]["items"]]

        assert labels == ["Аренда", "Еда"]

        # The chart and the table come from one aggregation, so the largest
        # bar is always the largest total and the shares always add up.
        assert view["chart"]["items"][0]["share"] == 100.0
        assert _total(view, 0) == "400"
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

        assert _total(view, 1) == "100"
        assert "50.0%" in view["totals"]["delta"]["text"]
        assert view["totals"]["delta"]["direction"] == "up"

    async def test_a_direction_is_sent_rather_than_a_signed_string(
        self, plugin, web_identity, food
    ):
        """The page colours the delta; it must not read the sign out of a
        formatted string to decide whether that is up or down."""
        plugin.repo.add(web_identity, food, Decimal("200"), date(2026, 4, 10))
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 5, 10))

        view = await plugin.page_action(web_identity, "month", {"month": "2026-05"})

        assert view["totals"]["delta"]["direction"] == "down"

    async def test_first_month_has_no_change_figure(
        self, plugin, web_identity, food
    ):
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 5, 10))

        view = await plugin.page_action(web_identity, "month", {"month": "2026-05"})

        assert view["totals"]["delta"] is None

    async def test_another_accounts_spending_is_absent(
        self, plugin, web_identity, food
    ):
        plugin.repo.add(web_identity, food, Decimal("500"), date(2026, 5, 1))

        view = await plugin.page_action(
            OTHER, "month", {"month": "2026-05", "screen": "analytics"}
        )

        assert _total(view, 0) == "0"
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

        spend = next(form for form in view["forms"] if form["id"] == "spend")

        assert spend["fields"][0]["options"] == []

    async def test_a_first_visit_opens_on_the_category_form(self, plugin, web_identity):
        view = await plugin.page_action(web_identity, "month", {})

        assert view["forms"][0]["id"] == "catadd"

    async def test_an_empty_month_says_so_once(self, plugin, web_identity):
        view = await plugin.page_action(web_identity, "month", {"screen": "analytics"})

        assert "empty" not in view["table"]


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

        assert _total(view, 0) == "77"


class TestDateFollowsTheMonth:
    """The date field and the month filter agree unless a person says
    otherwise."""

    def _date_field(self, view, form_id="spend"):
        for form in view["forms"]:
            if form["id"] == form_id:
                for field in form["fields"]:
                    if field["name"] == "spent_at":
                        return field
        raise AssertionError(f"no spent_at field in {form_id}")

    async def test_another_month_offers_its_first_day(self, plugin, web_identity):
        view = await plugin.page_action(
            web_identity, "month", {"month": "2026-09"}
        )

        assert self._date_field(view)["value"] == "2026-09-01"

    async def test_the_current_month_offers_today(
        self, plugin, web_identity
    ):
        today = date.today().strftime("%Y-%m")

        view = await plugin.page_action(web_identity, "month", {"month": today})

        assert self._date_field(view)["value"] == date.today().isoformat()

    async def test_the_date_is_marked_sticky(self, plugin, web_identity):
        """The month sets it; a hand-picked value has to survive a redraw."""
        view = await plugin.page_action(
            web_identity, "month", {"month": "2026-09"}
        )

        assert self._date_field(view)["sticky"] is True

    async def test_the_amount_is_not_sticky(self, plugin, web_identity):
        """Nothing to preserve about a figure that is retyped every time."""
        view = await plugin.page_action(
            web_identity, "month", {"month": "2026-09"}
        )
        amount = next(
            field
            for field in next(f for f in view["forms"] if f["id"] == "spend")["fields"]
            if field["name"] == "amount"
        )

        assert "sticky" not in amount


def _caption(view, caption: str) -> str:
    return next(
        item["value"] for item in view["totals"]["items"] if item["caption"] == caption
    )


class TestCategoriesOnThePage:
    async def test_a_removed_category_leaves_the_form_but_keeps_its_spending(
        self, plugin, web_identity, food
    ):
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 5, 3))

        view = await plugin.page_action(
            web_identity, "catarchive", {"category_id": str(food), "month": "2026-05"}
        )

        spend = next(form for form in view["forms"] if form["id"] == "spend")
        assert spend["fields"][0]["options"] == []
        assert view["list"][0]["rows"] == []
        assert _total(view, 0) == "100"

    async def test_rename(self, plugin, web_identity, food):
        view = await plugin.page_action(
            web_identity, "catrename", {"category_id": str(food)}
        )
        form = view["forms"][0]
        assert form["fields"][0]["value"] == "Еда"

        view = await plugin.page_action(
            web_identity, "catsave", {"category_id": str(food), "name": "Продукты"}
        )

        assert [row["name"] for row in view["list"][0]["rows"]] == ["Продукты"]

    async def test_a_taken_name_is_refused(self, plugin, web_identity, food):
        plugin.repo.add_category(web_identity, "Кафе")

        with pytest.raises(CommandError, match="уже есть"):
            await plugin.page_action(
                web_identity, "catsave", {"category_id": str(food), "name": "Кафе"}
            )

    async def test_another_accounts_category_cannot_be_renamed(self, plugin, food):
        with pytest.raises(CommandError, match="не найдена"):
            await plugin.page_action(
                OTHER, "catsave", {"category_id": str(food), "name": "Чужое"}
            )


class TestEditingAnExpense:
    async def test_the_form_opens_filled_and_saves(self, plugin, web_identity, food):
        plugin.repo.add(web_identity, food, Decimal("320"), date(2026, 5, 3), "кофе")
        expense_id = str(plugin.repo.recent(web_identity)[0]["id"])

        view = await plugin.page_action(
            web_identity, "edit", {"expense_id": expense_id, "month": "2026-05"}
        )
        [form] = view["forms"]
        values = {field["name"]: field.get("value") for field in form["fields"]}
        assert values["amount"] == "320.00"
        assert values["spent_at"] == "2026-05-03"
        assert values["expense_id"] == expense_id

        await plugin.page_action(
            web_identity,
            "spendsave",
            {**values, "amount": "350", "note": "кофе и булка"},
        )

        saved = plugin.repo.recent(web_identity)[0]
        assert (saved["amount"], saved["note"]) == (Decimal("350"), "кофе и булка")

    async def test_another_accounts_expense_is_not_changed(
        self, plugin, web_identity, food
    ):
        plugin.repo.add(web_identity, food, Decimal("320"), date(2026, 5, 3))
        expense_id = str(plugin.repo.recent(web_identity)[0]["id"])

        with pytest.raises(CommandError, match="не найдена"):
            await plugin.page_action(OTHER, "edit", {"expense_id": expense_id})
        with pytest.raises(CommandError, match="не найдена"):
            await plugin.page_action(
                OTHER,
                "spendsave",
                {"expense_id": expense_id, "category_id": str(food), "amount": "1"},
            )

        assert plugin.repo.recent(web_identity)[0]["amount"] == Decimal("320")

    async def test_an_infinite_amount_is_refused(self, plugin, web_identity, food):
        with pytest.raises(CommandError, match="больше нуля"):
            await plugin.page_action(
                web_identity, "spend", {"category_id": str(food), "amount": "Infinity"}
            )


class TestIncomes:
    async def test_two_results_tell_salary_from_help(self, plugin, web_identity, food):
        plugin.repo.add(web_identity, food, Decimal("30000"), date(2026, 5, 3))
        await plugin.page_action(
            web_identity,
            "income",
            {"kind": "salary", "amount": "25 000", "received_at": "2026-05-05"},
        )
        view = await plugin.page_action(
            web_identity,
            "income",
            {
                "kind": "other",
                "amount": "10000",
                "received_at": "2026-05-10",
                "month": "2026-05",
                "screen": "analytics",
            },
        )

        assert _caption(view, "Зарплата − траты") == "−5 000"
        assert _caption(view, "Все пополнения − траты") == "5 000"
        amounts = [row["amount"] for row in view["list"][0]["rows"]]
        assert amounts == ["+10 000", "+25 000", "30 000"]

    async def test_an_unknown_kind_is_refused(self, plugin, web_identity):
        with pytest.raises(CommandError, match="тип"):
            await plugin.page_action(
                web_identity, "income", {"kind": "gift", "amount": "1"}
            )

    async def test_edit_and_delete(self, plugin, web_identity):
        plugin.repo.add_income(web_identity, "other", Decimal("100"), date(2026, 5, 1))
        income_id = str(plugin.repo.incomes(web_identity, "2026-05")[0]["id"])

        [form] = (
            await plugin.page_action(
                web_identity, "incomeedit", {"income_id": income_id}
            )
        )["forms"]
        values = {field["name"]: field.get("value") for field in form["fields"]}
        await plugin.page_action(
            web_identity, "incomesave", {**values, "kind": "salary"}
        )
        assert plugin.repo.incomes(web_identity, "2026-05")[0]["kind"] == "salary"

        await plugin.page_action(
            web_identity, "incomedelete", {"income_id": income_id}
        )
        assert plugin.repo.incomes(web_identity, "2026-05") == []

    async def test_another_accounts_income_is_kept(self, plugin, web_identity):
        plugin.repo.add_income(web_identity, "other", Decimal("100"), date(2026, 5, 1))
        income_id = str(plugin.repo.incomes(web_identity, "2026-05")[0]["id"])

        await plugin.page_action(OTHER, "incomedelete", {"income_id": income_id})

        assert len(plugin.repo.incomes(web_identity, "2026-05")) == 1

    async def test_a_guest_is_capped(self, plugin, monkeypatch):
        from app.core.identity import guest

        monkeypatch.setattr("app.plugins.expenses.GUEST_INCOME_LIMIT", 1)
        visitor = guest("cap")
        await plugin.page_action(visitor, "income", {"kind": "other", "amount": "1"})

        with pytest.raises(CommandError, match="Демо ограничено"):
            await plugin.page_action(
                visitor, "income", {"kind": "other", "amount": "1"}
            )


class TestMonthList:
    async def test_holds_the_month_only_and_filters_by_category(
        self, plugin, web_identity, food
    ):
        cafe = plugin.repo.add_category(web_identity, "Кафе")
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 5, 3))
        plugin.repo.add(web_identity, cafe, Decimal("200"), date(2026, 5, 4))
        plugin.repo.add(web_identity, food, Decimal("300"), date(2026, 4, 30))

        view = await plugin.page_action(
            web_identity, "month", {"month": "2026-05", "screen": "analytics"}
        )
        assert [row["amount"] for row in view["list"][0]["rows"]] == ["200", "100"]

        view = await plugin.page_action(
            web_identity,
            "filter",
            {"category_id": str(food), "month": "2026-05", "screen": "analytics"},
        )
        listed = view["list"][0]
        assert [row["amount"] for row in listed["rows"]] == ["100"]
        assert listed["title"] == "Траты: Еда, май 2026"
        assert listed["tools"][0]["action"] == "month"


class TestScreens:
    async def test_entry_holds_totals_forms_and_categories(self, plugin, web_identity):
        view = await plugin.page_view(web_identity)

        assert view["totals"]["items"]
        assert "chart" not in view and "table" not in view
        assert view["list"][0]["title"] == "Категории"
        assert view["keep"]["screen"] == "entry"

    async def test_analytics_holds_totals_chart_and_operations(
        self, plugin, web_identity
    ):
        view = await plugin.page_action(
            web_identity, "month", {"screen": "analytics"}
        )

        assert view["totals"]["items"]
        assert view["chart"] and view["table"]
        assert view["list"][0]["title"].startswith("Операции за")
        assert view["forms"] == []
        assert view["tools"][0]["params"]["screen"] == "entry"

    async def test_an_edit_from_the_list_stays_on_analytics(
        self, plugin, web_identity, food
    ):
        plugin.repo.add(web_identity, food, Decimal("320"), date(2026, 5, 3))
        expense_id = str(plugin.repo.recent(web_identity)[0]["id"])

        view = await plugin.page_action(
            web_identity,
            "edit",
            {"expense_id": expense_id, "month": "2026-05", "screen": "analytics"},
        )

        assert view["keep"]["screen"] == "analytics"
        assert view["forms"][0]["id"] == "spendsave"
        assert "chart" in view

    async def test_an_unknown_screen_falls_back_to_entry(self, plugin, web_identity):
        view = await plugin.page_action(web_identity, "month", {"screen": "x"})

        assert view["keep"]["screen"] == "entry"


class TestReviewFixes:
    @pytest.mark.parametrize(
        "amount, message",
        [("0,004", "двух знаков"), ("10000000000", "Слишком большая")],
    )
    async def test_an_amount_the_column_cannot_hold_is_refused(
        self, plugin, web_identity, food, amount, message
    ):
        with pytest.raises(CommandError, match=message):
            await plugin.page_action(
                web_identity, "spend", {"category_id": str(food), "amount": amount}
            )

    async def test_a_note_must_be_text(self, plugin, web_identity):
        with pytest.raises(CommandError, match="текстом"):
            await plugin.page_action(
                web_identity, "income", {"kind": "other", "amount": "1", "note": 5}
            )

    async def test_the_filter_survives_a_delete(self, plugin, web_identity, food):
        cafe = plugin.repo.add_category(web_identity, "Кафе")
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 5, 3))
        plugin.repo.add(web_identity, food, Decimal("150"), date(2026, 5, 4))
        plugin.repo.add(web_identity, cafe, Decimal("200"), date(2026, 5, 4))
        view = await plugin.page_action(
            web_identity,
            "filter",
            {"category_id": str(food), "month": "2026-05", "screen": "analytics"},
        )
        first = view["list"][0]["rows"][0]["actions"][1]["params"]

        view = await plugin.page_action(
            web_identity, "delete", {**view["keep"], **first}
        )

        assert [row["amount"] for row in view["list"][0]["rows"]] == ["100"]
        show_all = view["list"][0]["tools"][0]["params"]
        view = await plugin.page_action(
            web_identity, "month", {**view["keep"], **show_all}
        )
        assert len(view["list"][0]["rows"]) == 2

    async def test_adding_a_removed_name_brings_it_back(
        self, plugin, web_identity, food
    ):
        plugin.repo.add(web_identity, food, Decimal("100"), date(2026, 5, 3))
        await plugin.page_action(web_identity, "catarchive", {"category_id": str(food)})

        await plugin.page_action(web_identity, "catadd", {"name": "Еда"})

        [category] = plugin.repo.categories(web_identity)
        assert (category["id"], category["expenses_count"]) == (food, 1)
