from datetime import date
from decimal import Decimal

import pytest

from app.core.identity import TELEGRAM, WEB, Identity
from app.core.results import CommandError
from app.plugins.applications import ApplicationsPlugin
from app.plugins.expenses import ExpensesPlugin
from app.plugins.todo import TodoPlugin

WEB_SIDE = Identity(kind=WEB, id="alexey")
CHAT = Identity(kind=TELEGRAM, id="555")
STRANGER = Identity(kind=WEB, id="someone")


class Pair:
    def identities(self, identity):
        return (WEB_SIDE, CHAT) if identity in (WEB_SIDE, CHAT) else (identity,)


@pytest.fixture
def todo(database_with_schema, notifier):
    return TodoPlugin(notifier, database_with_schema, persons=Pair())


@pytest.fixture
def applications(database_with_schema):
    return ApplicationsPlugin(database_with_schema, persons=Pair())


@pytest.fixture
def expenses(database_with_schema):
    return ExpensesPlugin(database_with_schema, persons=Pair())


class TestTasks:
    async def test_a_task_from_the_chat_is_edited_on_the_web(self, todo):
        todo.repo.add(CHAT, "из Telegram")
        [task] = todo.repo.entries(CHAT)

        task_id = str(task["id"])
        await todo.page_action(WEB_SIDE, "priority", {"id": task_id, "priority": "1"})
        await todo.page_action(WEB_SIDE, "move", {"id": task_id, "status": "doing"})

        [task] = todo.repo.entries(CHAT)
        assert (task["priority"], task["status"]) == (1, "doing")

        await todo.page_action(WEB_SIDE, "delete", {"id": str(task["id"])})
        assert todo.repo.entries(CHAT) == []

    async def test_a_stranger_still_cannot(self, todo):
        todo.repo.add(CHAT, "из Telegram")
        [task] = todo.repo.entries(CHAT)

        with pytest.raises(CommandError, match="не найдена"):
            await todo.page_action(STRANGER, "delete", {"id": str(task["id"])})


class TestApplications:
    async def test_the_stage_moves_across_the_pair(self, applications):
        entry = applications.repo.add(CHAT, "Компания", "Аналитик", date.today())

        await applications.page_action(
            WEB_SIDE, "stage", {"id": str(entry), "status": "answered"}
        )

        [row] = applications.repo.entries(CHAT)
        assert row["status"] == "answered"

        with pytest.raises(CommandError):
            await applications.page_action(STRANGER, "delete", {"id": str(entry)})


class TestExpenses:
    async def test_an_income_from_the_pair_is_removed(self, expenses):
        expenses.repo.add_income(CHAT, "other", Decimal("100"), date.today())
        month = date.today().strftime("%Y-%m")
        [income] = expenses.repo.incomes(CHAT, month)

        assert expenses.repo.get_income(STRANGER, income["id"]) is None

        await expenses.page_action(
            WEB_SIDE, "incomedelete", {"income_id": str(income["id"]), "month": month}
        )

        assert expenses.repo.incomes(CHAT, month) == []

    async def test_an_expense_keeps_a_category_of_its_own_owner(self, expenses):
        chat_food = expenses.repo.add_category(CHAT, "Еда")
        web_fun = expenses.repo.add_category(WEB_SIDE, "Досуг")
        expenses.repo.add(CHAT, chat_food, Decimal("50"), date.today())
        month = date.today().strftime("%Y-%m")
        [entry] = expenses.repo.recent(CHAT, month=month)

        assert not expenses.repo.update(
            (WEB_SIDE, CHAT), entry["id"], web_fun, Decimal("60"), date.today()
        )
        assert expenses.repo.update(
            (WEB_SIDE, CHAT), entry["id"], chat_food, Decimal("60"), date.today()
        )
