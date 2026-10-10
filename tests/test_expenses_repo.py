from datetime import date
from decimal import Decimal

import pytest

from app.core.identity import WEB, Identity, telegram
from app.db.expenses_repo import ExpensesRepository

OTHER = Identity(kind=WEB, id="someone-else")


@pytest.fixture
def repo(database_with_schema):
    return ExpensesRepository(database_with_schema)


class TestCategories:
    def test_adds_and_lists_in_order(self, repo, web_identity):
        food = repo.add_category(web_identity, "Еда")
        transport = repo.add_category(web_identity, "Транспорт")

        listed = repo.categories(web_identity)

        assert [c["name"] for c in listed] == ["Еда", "Транспорт"]
        assert listed[0]["id"] == food
        assert listed[1]["id"] == transport

    def test_two_accounts_never_see_each_other(self, repo, web_identity):
        repo.add_category(web_identity, "Еда")
        repo.add_category(OTHER, "Ипотека")

        assert [c["name"] for c in repo.categories(web_identity)] == ["Еда"]
        assert [c["name"] for c in repo.categories(OTHER)] == ["Ипотека"]

    def test_same_name_allowed_across_accounts(self, repo, web_identity):
        """A name is unique per account, not globally.

        Two people keeping the same budget must not collide, and a shared
        dictionary is the same leak the notes table avoids.
        """
        repo.add_category(web_identity, "Еда")
        repo.add_category(OTHER, "Еда")

        assert len(repo.categories(web_identity)) == 1
        assert len(repo.categories(OTHER)) == 1

    def test_duplicate_active_name_rejected(self, repo, web_identity):
        repo.add_category(web_identity, "Еда")

        with pytest.raises(Exception):
            repo.add_category(web_identity, "Еда")

    def test_archive_hides_it_and_frees_the_name(self, repo, web_identity):
        first = repo.add_category(web_identity, "Еда")
        repo.archive_category(web_identity, first)

        assert repo.categories(web_identity) == []

        repo.add_category(web_identity, "Еда")

        assert [c["name"] for c in repo.categories(web_identity)] == ["Еда"]

    def test_archived_still_reported_when_asked(self, repo, web_identity):
        first = repo.add_category(web_identity, "Еда")
        repo.archive_category(web_identity, first)

        listed = repo.categories(web_identity, include_archived=True)

        assert len(listed) == 1
        assert listed[0]["is_archived"] is True

    def test_cannot_archive_someone_elses_category(self, repo, web_identity):
        theirs = repo.add_category(OTHER, "Ипотека")

        assert repo.archive_category(web_identity, theirs) is False
        assert repo.archive_category(OTHER, theirs) is True

    def test_archiving_keeps_the_expenses(self, repo, web_identity):
        """A delete would cascade and silently rewrite a month."""
        food = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, food, Decimal("100.00"), date(2026, 5, 3))
        repo.archive_category(web_identity, food)

        assert repo.recent(web_identity) != []
        assert repo.monthly_total(web_identity, "2026-05") == Decimal("100.00")


class TestExpenses:
    def test_adds_and_reads_back(self, repo, web_identity):
        food = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, food, Decimal("320.50"), date(2026, 5, 3))

        listed = repo.recent(web_identity)

        assert listed[0]["amount"] == Decimal("320.50")
        assert listed[0]["category"] == "Еда"
        assert listed[0]["spent_at"] == date(2026, 5, 3)

    def test_amount_stays_exact(self, repo, web_identity):
        """Ten hundredths must not become nine.

        This is the whole reason the column is NUMERIC. The assertion is on
        exact equality, so a float creeping in anywhere on the path fails here
        rather than in a monthly total nobody reads closely.
        """
        food = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, food, Decimal("0.10"), date(2026, 5, 1))

        assert repo.recent(web_identity)[0]["amount"] == Decimal("0.10")

        total = Decimal("0.00")
        for _ in range(10):
            total += Decimal("0.10")

        assert total == Decimal("1.00")
        assert repo.monthly_total(web_identity, "2026-05") == Decimal("0.10")

    def test_rejects_another_accounts_category(self, repo, web_identity):
        """The id is a global sequence; accepting it would file the expense
        in someone else's totals."""
        theirs = repo.add_category(OTHER, "Ипотека")

        with pytest.raises(ValueError):
            repo.add(web_identity, theirs, Decimal("50"), date(2026, 5, 1))

        assert repo.recent(OTHER) == []

    def test_recent_is_scoped_to_the_account(self, repo, web_identity):
        mine = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, mine, Decimal("100"), date(2026, 5, 1))

        assert repo.recent(OTHER) == []

    def test_delete_is_scoped_to_the_account(self, repo, web_identity):
        mine = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, mine, Decimal("100"), date(2026, 5, 1))
        expense_id = repo.recent(web_identity)[0]["id"]

        assert repo.delete(OTHER, expense_id) is False
        assert repo.delete(web_identity, expense_id) is True

    def test_spent_at_not_created_at_drives_the_month(self, repo, web_identity):
        """Entered late means spent earlier.

        A total that follows the entry date rather than the spending date
        answers a different question than the one being asked.
        """
        food = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, food, Decimal("500"), date(2026, 4, 28))

        assert repo.monthly_total(web_identity, "2026-04") == Decimal("500")
        assert repo.monthly_total(web_identity, "2026-05") == Decimal("0")

    def test_last_day_of_a_short_month_belongs_to_it(self, repo, web_identity):
        """February has 28 days in 2026. A range closed with a fixed day count
        would drop the 28th into the next month."""
        food = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, food, Decimal("70"), date(2026, 2, 28))

        assert repo.monthly_total(web_identity, "2026-02") == Decimal("70")

    def test_first_day_of_the_next_month_does_not_leak_back(
        self, repo, web_identity
    ):
        food = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, food, Decimal("70"), date(2026, 3, 1))

        assert repo.monthly_total(web_identity, "2026-02") == Decimal("0")
        assert repo.monthly_total(web_identity, "2026-03") == Decimal("70")


class TestAggregation:
    def test_groups_by_category_largest_first(self, repo, web_identity):
        food = repo.add_category(web_identity, "Еда")
        rent = repo.add_category(web_identity, "Аренда")
        repo.add(web_identity, food, Decimal("100"), date(2026, 5, 1))
        repo.add(web_identity, food, Decimal("50"), date(2026, 5, 2))
        repo.add(web_identity, rent, Decimal("1000"), date(2026, 5, 1))

        rows = repo.by_category(web_identity, "2026-05")

        assert [r["category"] for r in rows] == ["Аренда", "Еда"]
        assert rows[1]["total"] == Decimal("150")
        assert rows[1]["count"] == 2

    def test_totals_agree_with_the_sum_of_the_groups(self, repo, web_identity):
        food = repo.add_category(web_identity, "Еда")
        rent = repo.add_category(web_identity, "Аренда")
        repo.add(web_identity, food, Decimal("120.10"), date(2026, 5, 1))
        repo.add(web_identity, rent, Decimal("1000.00"), date(2026, 5, 2))

        grouped = sum(r["total"] for r in repo.by_category(web_identity, "2026-05"))

        assert grouped == repo.monthly_total(web_identity, "2026-05")
        assert grouped == Decimal("1120.10")

    def test_empty_month_is_zero_not_missing(self, repo, web_identity):
        assert repo.by_category(web_identity, "2026-05") == []
        assert repo.monthly_total(web_identity, "2026-05") == Decimal("0")

    def test_available_months_newest_first(self, repo, web_identity):
        food = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, food, Decimal("10"), date(2026, 3, 5))
        repo.add(web_identity, food, Decimal("10"), date(2026, 5, 5))
        repo.add(web_identity, food, Decimal("10"), date(2026, 5, 20))

        assert repo.available_months(web_identity) == ["2026-05", "2026-03"]

    def test_one_accounts_spending_is_not_in_anothers_report(
        self, repo, web_identity
    ):
        mine = repo.add_category(web_identity, "Еда")
        repo.add(web_identity, mine, Decimal("500"), date(2026, 5, 1))

        assert repo.by_category(OTHER, "2026-05") == []
        assert repo.monthly_total(OTHER, "2026-05") == Decimal("0")
        assert repo.available_months(OTHER) == []


class TestWidening:
    """A linked pair is read as one person, the same rule notes and clipboard
    follow. Writes are not widened."""

    @pytest.fixture
    def pair(self, repo):
        web = Identity(kind=WEB, id="alexey")
        chat = Identity(kind=telegram.__name__.split(".")[0], id="555")
        return [web, chat], web

    def test_categories_are_shared_across_the_pair(self, repo, pair):
        identities, web = pair
        food = repo.add_category(web, "Еда")

        names = [c["name"] for c in repo.categories(identities)]

        assert names == ["Еда"]
        assert repo.monthly_total(identities, "2026-05") == Decimal("0")
        assert food is not None

    def test_an_expense_written_on_one_side_shows_on_the_other(
        self, repo, pair
    ):
        identities, web = pair
        food = repo.add_category(web, "Еда")
        repo.add(web, food, Decimal("77"), date(2026, 5, 1))

        assert repo.by_category(identities, "2026-05")[0]["total"] == Decimal("77")
