import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.identity import WEB, Identity, guest
from app.core.results import CommandError
from app.plugins.automation import AutomationPlugin
from app.plugins.expenses import ExpensesPlugin
from app.plugins.manager import PluginManager
from app.plugins.todo import TodoPlugin

OTHER = Identity(kind=WEB, id="someone-else")


@pytest.fixture
def platform(database_with_schema, notifier):
    manager = PluginManager(logging.getLogger("test"))
    todo = TodoPlugin(notifier, database_with_schema)
    expenses = ExpensesPlugin(database_with_schema)
    automation = AutomationPlugin(manager, notifier, database_with_schema)
    for plugin in (todo, expenses, automation):
        manager.register(plugin)
    return manager, todo, expenses, automation


def _texts(todo, identity):
    return [task["text"] for task in todo.repo.entries(identity)]


async def _spend(expenses, identity, category_id, amount):
    await expenses.page_action(
        identity, "spend", {"category_id": str(category_id), "amount": amount}
    )


async def test_a_large_expense_makes_a_task(platform, web_identity):
    _, todo, expenses, automation = platform
    food = expenses.repo.add_category(web_identity, "Продукты")
    automation.repo.add(
        web_identity,
        "Крупная трата",
        "expense.added",
        "task.create",
        {"text": "Проверить {сумма} в {категория}"},
        condition_amount=Decimal("5000"),
    )

    await _spend(expenses, web_identity, food, "300")
    await _spend(expenses, web_identity, food, "6200")

    assert _texts(todo, web_identity) == ["Проверить 6 200 в Продукты"]
    [run] = automation.repo.runs(web_identity)
    assert run["ok"] and "Проверить" in run["message"]


async def test_a_text_condition_must_match(platform, web_identity):
    _, todo, _, automation = platform
    automation.repo.add(
        web_identity,
        "Оплата",
        "task.done",
        "task.create",
        {"text": "Следом"},
        condition_text="оплат",
    )
    todo.repo.add(web_identity, "Позвонить", status="doing")
    task_id = str(todo.repo.entries(web_identity)[0]["id"])

    await todo.page_action(web_identity, "move", {"id": task_id, "status": "done"})

    assert "Следом" not in _texts(todo, web_identity)
    assert automation.repo.runs(web_identity) == []


async def test_rules_chain_across_plugins(platform, web_identity):
    _, todo, expenses, automation = platform
    expenses.repo.add_category(web_identity, "Коммунальные")
    await automation.seed_demo(web_identity)
    todo.repo.add(web_identity, "Оплатить коммунальные", status="doing")
    task_id = str(todo.repo.entries(web_identity)[0]["id"])

    await todo.page_action(web_identity, "move", {"id": task_id, "status": "done"})

    assert expenses.repo.monthly_total(
        web_identity, date.today().strftime("%Y-%m")
    ) == Decimal("8400")
    assert any(
        text.startswith("Проверить крупную трату")
        for text in _texts(todo, web_identity)
    )
    assert [run["ok"] for run in automation.repo.runs(web_identity)] == [True, True]


async def test_a_rule_cannot_feed_itself(platform, web_identity):
    _, todo, _, automation = platform
    automation.repo.add(
        web_identity, "Эхо", "task.created", "task.create", {"text": "ещё {текст}"}
    )

    await todo.page_action(web_identity, "add", {"text": "раз"})

    assert sorted(_texts(todo, web_identity)) == ["ещё раз", "раз"]
    runs = automation.repo.runs(web_identity)
    assert [run["ok"] for run in runs] == [True, False]
    assert "цепочку" in runs[1]["message"]


async def test_a_disabled_rule_is_silent(platform, web_identity):
    _, todo, _, automation = platform
    rule_id = automation.repo.add(
        web_identity, "Эхо", "task.created", "task.create", {"text": "копия"}
    )

    await automation.page_action(
        web_identity, "toggle", {"rule_id": str(rule_id), "enabled": "0"}
    )
    await todo.page_action(web_identity, "add", {"text": "раз"})

    assert _texts(todo, web_identity) == ["раз"]


class TestSchedule:
    def _weekly(self, automation, identity):
        automation.repo.add(
            identity,
            "Почта",
            "schedule",
            "task.create",
            {"text": "Разобрать почту"},
            schedule={"period": "weekly", "day": 1, "hour": 10},
        )

    async def test_fires_once_on_its_day_from_its_hour(
        self, platform, web_identity, monkeypatch
    ):
        monkeypatch.setattr("app.core.config.Config.DISPLAY_TIMEZONE", "UTC")
        _, todo, _, automation = platform
        self._weekly(automation, web_identity)

        await automation.process_schedule(datetime(2026, 11, 9, 9, 0, tzinfo=UTC))
        assert _texts(todo, web_identity) == []

        await automation.process_schedule(datetime(2026, 11, 9, 10, 5, tzinfo=UTC))
        await automation.process_schedule(datetime(2026, 11, 9, 15, 0, tzinfo=UTC))
        assert _texts(todo, web_identity) == ["Разобрать почту"]

        await automation.process_schedule(datetime(2026, 11, 10, 10, 5, tzinfo=UTC))
        assert len(_texts(todo, web_identity)) == 1

    async def test_the_31st_falls_on_the_last_day(
        self, platform, web_identity, monkeypatch
    ):
        monkeypatch.setattr("app.core.config.Config.DISPLAY_TIMEZONE", "UTC")
        _, todo, _, automation = platform
        automation.repo.add(
            web_identity,
            "Конец месяца",
            "schedule",
            "task.create",
            {"text": "Итоги месяца"},
            schedule={"period": "monthly", "day": 31, "hour": 9},
        )

        await automation.process_schedule(datetime(2027, 2, 28, 9, 0, tzinfo=UTC))

        assert _texts(todo, web_identity) == ["Итоги месяца"]


class TestPage:
    async def test_two_steps_make_a_rule(self, platform, web_identity):
        _, _, _, automation = platform

        view = await automation.page_action(
            web_identity,
            "draft",
            {"name": "Крупная", "trigger": "expense.added", "action": "task.create"},
        )
        [form] = view["forms"]
        names = [field["name"] for field in form["fields"]]
        assert names[:2] == ["condition_text", "condition_amount"]

        values = {field["name"]: field.get("value", "") for field in form["fields"]}
        view = await automation.page_action(
            web_identity,
            "create",
            {**values, "condition_amount": "5 000", "text": "Проверить {сумма}"},
        )

        [row] = view["list"]["rows"]
        assert row["condition"] == "сумма больше 5 000"
        assert row["fired"] == "ещё нет"

    async def test_an_unknown_action_is_refused(self, platform, web_identity):
        _, _, _, automation = platform

        with pytest.raises(CommandError, match="что делает"):
            await automation.page_action(
                web_identity,
                "create",
                {"name": "x", "trigger": "task.done", "action": "rm.rf"},
            )

    async def test_a_schedule_day_is_checked(self, platform, web_identity):
        _, _, _, automation = platform

        with pytest.raises(CommandError, match="День недели"):
            await automation.page_action(
                web_identity,
                "create",
                {
                    "name": "x",
                    "trigger": "schedule",
                    "action": "task.create",
                    "period": "weekly",
                    "day": "8",
                    "hour": "9",
                    "text": "y",
                },
            )

    async def test_another_account_cannot_delete(self, platform, web_identity):
        _, _, _, automation = platform
        rule_id = automation.repo.add(
            web_identity, "Моё", "task.done", "task.create", {"text": "x"}
        )

        with pytest.raises(CommandError, match="не найдено"):
            await automation.page_action(OTHER, "delete", {"rule_id": str(rule_id)})

        assert len(automation.repo.entries(web_identity)) == 1

    async def test_a_guest_is_capped(self, platform, monkeypatch):
        _, _, _, automation = platform
        monkeypatch.setattr("app.plugins.automation.GUEST_RULE_LIMIT", 1)
        visitor = guest("cap")
        payload = {
            "name": "x",
            "trigger": "task.done",
            "action": "task.create",
            "text": "y",
        }
        await automation.page_action(visitor, "create", payload)

        with pytest.raises(CommandError, match="Демо ограничено"):
            await automation.page_action(visitor, "create", payload)

    async def test_the_journal_shows_a_failed_telegram_message(
        self, platform, web_identity
    ):
        _, todo, _, automation = platform
        automation.repo.add(
            web_identity,
            "Сообщить",
            "task.created",
            "telegram.send",
            {"text": "{текст}"},
        )
        await todo.page_action(web_identity, "add", {"text": "раз"})

        view = await automation.page_action(web_identity, "view", {"screen": "journal"})

        [row] = view["list"]["rows"]
        assert row["result"] == "✗"
        assert "Некуда отправить" in row["message"]


async def test_the_demo_has_examples(platform):
    _, _, _, automation = platform
    visitor = guest("demo")

    await automation.seed_demo(visitor)

    assert len(automation.repo.entries(visitor)) == 3


async def test_the_list_names_the_fields_in_their_order(platform, web_identity):
    _, _, _, automation = platform
    automation.repo.add(
        web_identity,
        "Коммуналка",
        "task.done",
        "expense.add",
        {"category": "Коммунальные", "amount": "8400", "note": "{текст}"},
    )

    view = await automation.page_view(web_identity)

    assert view["list"]["rows"][0]["what"] == (
        "Внести трату: Коммунальные · 8400 · {текст}"
    )


async def test_switched_off_in_the_admin_area_nothing_fires(platform, web_identity):
    manager, todo, _, automation = platform

    class Switches:
        def disabled_plugins(self):
            return {"automation"}

        def maintenance(self):
            return False

    manager.switches = Switches()
    automation.repo.add(
        web_identity, "Эхо", "task.created", "task.create", {"text": "копия"}
    )

    await todo.page_action(web_identity, "add", {"text": "раз"})

    assert _texts(todo, web_identity) == ["раз"]


class TestFailures:
    async def test_a_crashing_action_is_logged_and_the_rest_still_run(
        self, platform, web_identity, monkeypatch
    ):
        _, todo, expenses, automation = platform
        food = expenses.repo.add_category(web_identity, "Продукты")
        for name in ("Первое", "Второе"):
            automation.repo.add(
                web_identity, name, "expense.added", "task.create", {"text": name}
            )
        real = todo.automation
        calls = []

        async def flaky(identity, action, params, chain):
            calls.append(params["text"])
            if len(calls) == 1:
                raise RuntimeError("boom")
            return await real(identity, action, params, chain)

        monkeypatch.setattr(todo, "automation", flaky)

        await _spend(expenses, web_identity, food, "100")

        assert len(expenses.repo.recent(web_identity)) == 1
        assert len(calls) == 2
        assert len(_texts(todo, web_identity)) == 1
        runs = automation.repo.runs(web_identity)
        assert sorted(run["ok"] for run in runs) == [False, True]
        assert "Внутренняя ошибка." in [run["message"] for run in runs]
        assert "boom" not in " ".join(run["message"] for run in runs)

    async def test_a_broken_rule_store_does_not_break_the_users_action(
        self, platform, web_identity, monkeypatch
    ):
        _, _, expenses, automation = platform
        food = expenses.repo.add_category(web_identity, "Продукты")

        def down(*args):
            raise RuntimeError("database is down")

        monkeypatch.setattr(automation.repo, "listening", down)

        await _spend(expenses, web_identity, food, "100")

        assert len(expenses.repo.recent(web_identity)) == 1

    async def test_a_failed_schedule_pass_does_not_stop_the_worker(
        self, platform, monkeypatch
    ):
        import asyncio

        from app.plugins import automation as module

        _, _, _, automation = platform
        passes = []

        async def failing(now):
            passes.append(now)
            if len(passes) == 3:
                raise asyncio.CancelledError
            raise RuntimeError("boom")

        async def instant(seconds):
            return None

        monkeypatch.setattr(automation, "process_schedule", failing)
        monkeypatch.setattr(module.asyncio, "sleep", instant)

        with pytest.raises(asyncio.CancelledError):
            await automation._worker()

        assert len(passes) == 3
