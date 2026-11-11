import asyncio
from datetime import datetime, timedelta

import pytest

from app.core.identity import WEB, Identity, guest, telegram
from app.core.results import CommandError
from app.plugins.todo import STATUS_LABELS, TodoPlugin, _local_now, _midnight


class RecordingChannel:
    def __init__(self, kind: str):
        self.kind = kind
        self.received = []

    async def deliver(self, target, text, **_):
        self.received.append((target, text))
        return True


@pytest.fixture
def plugin(database_with_schema, notifier):
    return TodoPlugin(notifier, database_with_schema)


class TestView:
    async def test_view_returns_three_columns(self, plugin, web_identity):
        view = await plugin.page_view(web_identity)

        assert view["title"] == "Задачи"
        assert [c["label"] for c in view["kanban"]["columns"]] == [
            STATUS_LABELS["todo"],
            STATUS_LABELS["doing"],
            STATUS_LABELS["done"],
        ]

    async def test_the_sort_order_is_honoured(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Без приоритета")
        plugin.repo.add(web_identity, "Высокий", priority=1)

        by_priority = await plugin.page_action(
            web_identity, "view", {"order": "priority"}
        )
        todo = by_priority["kanban"]["columns"][0]["cards"]

        assert [c["text"] for c in todo] == ["Высокий", "Без приоритета"]

    async def test_a_new_card_only_moves_forward(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Задача")

        view = await plugin.page_view(web_identity)
        card = view["kanban"]["columns"][0]["cards"][0]

        assert card["moves"] == [
            {"status": "doing", "label": "В работе", "dir": "forward"}
        ]

    async def test_a_done_card_only_moves_back(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Задача")
        task_id = plugin.repo.entries(web_identity)[0]["id"]
        plugin.repo.set_status(web_identity, task_id, "done")

        view = await plugin.page_view(web_identity)
        done = view["kanban"]["columns"][2]["cards"][0]

        assert done["moves"] == [
            {"status": "doing", "label": "В работе", "dir": "back"}
        ]

    async def test_tasks_land_in_the_right_column(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Сделать")
        task_id = plugin.repo.entries(web_identity)[0]["id"]
        plugin.repo.set_status(web_identity, task_id, "doing")

        view = await plugin.page_view(web_identity)
        columns = view["kanban"]["columns"]

        assert columns[0]["cards"] == []
        assert [c["text"] for c in columns[1]["cards"]] == ["Сделать"]

    async def test_overdue_is_marked(self, plugin, web_identity):
        plugin.repo.add(
            web_identity, "Просрочена", due_at=datetime.now() - timedelta(days=1)
        )

        view = await plugin.page_view(web_identity)
        card = view["kanban"]["columns"][0]["cards"][0]

        assert card["overdue"] is True


class TestMove:
    async def test_move_follows_the_state_machine(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Задача")
        task_id = plugin.repo.entries(web_identity)[0]["id"]

        # todo -> doing is allowed.
        await plugin.page_action(
            web_identity, "move", {"id": str(task_id), "status": "doing"}
        )
        assert plugin.repo.entries(web_identity)[0]["status"] == "doing"

        # doing -> done is allowed (the next step).
        await plugin.page_action(
            web_identity, "move", {"id": str(task_id), "status": "done"}
        )
        assert plugin.repo.entries(web_identity)[0]["status"] == "done"

    async def test_a_jump_over_a_status_is_refused(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Задача")
        task_id = plugin.repo.entries(web_identity)[0]["id"]

        # todo -> done skips "doing" and must be refused.
        with pytest.raises(Exception, match="Нельзя перевести"):
            await plugin.page_action(
                web_identity, "move", {"id": str(task_id), "status": "done"}
            )

    async def test_add_refuses_empty_text(self, plugin, web_identity):
        with pytest.raises(Exception, match="не может быть пустым"):
            await plugin.page_action(web_identity, "add", {"text": "   "})

    async def test_move_accepts_an_integer_id(self, plugin, web_identity):
        """The browser sends numbers in JSON, not strings.

        A task id that arrives as an int used to crash the handler with an
        AttributeError and surface as a 500 on the page."""
        plugin.repo.add(web_identity, "Задача")
        task_id = plugin.repo.entries(web_identity)[0]["id"]

        await plugin.page_action(
            web_identity, "move", {"id": task_id, "status": "doing"}
        )

        assert plugin.repo.entries(web_identity)[0]["status"] == "doing"

    async def test_delete_accepts_an_integer_id(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Задача")
        task_id = plugin.repo.entries(web_identity)[0]["id"]

        await plugin.page_action(web_identity, "delete", {"id": task_id})

        assert plugin.repo.entries(web_identity) == []


MORNING = datetime(2026, 11, 12, 9, 30)
DUE_DAY = datetime(2026, 11, 12)


@pytest.fixture
def clock(monkeypatch):
    def set_to(moment):
        monkeypatch.setattr("app.plugins.todo._local_now", lambda: moment)

    set_to(MORNING)
    return set_to


@pytest.fixture
def sent(plugin):
    channel = RecordingChannel("telegram")
    plugin._notifier._channels["telegram"] = channel
    return channel


class TestWorker:
    async def test_process_due_delivers_once(self, plugin, sent, clock):
        plugin.repo.add(telegram(4242), "Напомнить", due_at=DUE_DAY)

        await plugin.process_due()

        assert len(sent.received) == 1
        assert "Напомнить" in sent.received[0][1]

        await plugin.process_due()
        assert len(sent.received) == 1

    async def test_a_deadline_tomorrow_waits_for_its_day(self, plugin, sent, clock):
        plugin.repo.add(
            telegram(4242), "Завтра", due_at=DUE_DAY + timedelta(days=1)
        )

        await plugin.process_due()
        assert sent.received == []

        clock(MORNING + timedelta(days=1))
        await plugin.process_due()
        assert len(sent.received) == 1

    async def test_nothing_is_sent_before_the_morning(self, plugin, sent, clock):
        plugin.repo.add(telegram(4242), "Сегодня", due_at=DUE_DAY)

        clock(DUE_DAY.replace(hour=8, minute=59))
        await plugin.process_due()
        assert sent.received == []

        clock(DUE_DAY.replace(hour=9))
        await plugin.process_due()
        assert len(sent.received) == 1

    async def test_an_old_deadline_is_not_dug_up(self, plugin, sent, clock):
        plugin.repo.add(
            telegram(4242), "Давно", due_at=DUE_DAY - timedelta(days=1)
        )

        await plugin.process_due()

        assert sent.received == []

    async def test_the_day_follows_the_display_zone(self, monkeypatch):
        from app.core.config import Config
        from app.plugins.todo import _local_now

        monkeypatch.setattr(Config, "DISPLAY_TIMEZONE", "Europe/Moscow")
        moscow = _local_now()
        monkeypatch.setattr(Config, "DISPLAY_TIMEZONE", "UTC")

        assert timedelta(hours=2, minutes=59) < moscow - _local_now() < timedelta(
            hours=3, minutes=1
        )

    async def test_a_moved_deadline_is_reminded_again(self, plugin, sent, clock):
        owner = telegram(4242)
        plugin.repo.add(owner, "Напомнить", due_at=DUE_DAY)
        task_id = plugin.repo.entries(owner)[0]["id"]
        await plugin.process_due()

        plugin.repo.set_due(owner, task_id, DUE_DAY)
        await plugin.process_due()
        assert len(sent.received) == 1

        moved = DUE_DAY + timedelta(days=2)
        plugin.repo.set_due(owner, task_id, moved)
        clock(moved.replace(hour=10))
        await plugin.process_due()
        assert len(sent.received) == 2



def _cards(view, column: int) -> list[dict]:
    return view["kanban"]["columns"][column]["cards"]


class TestEditing:
    async def test_text_and_description(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Купть хлеб")
        task_id = str(plugin.repo.entries(web_identity)[0]["id"])

        view = await plugin.page_action(
            web_identity,
            "text",
            {"id": task_id, "text": "Купить хлеб", "description": "Бородинский"},
        )

        card = _cards(view, 0)[0]
        assert (card["text"], card["description"]) == ("Купить хлеб", "Бородинский")

    async def test_empty_text_is_refused(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Задача")
        task_id = str(plugin.repo.entries(web_identity)[0]["id"])

        with pytest.raises(CommandError, match="пустым"):
            await plugin.page_action(web_identity, "text", {"id": task_id, "text": " "})

    async def test_another_accounts_task_is_not_changed(self, plugin, web_identity):
        plugin.repo.add(web_identity, "Моя")
        task_id = str(plugin.repo.entries(web_identity)[0]["id"])
        other = Identity(kind=WEB, id="someone-else")

        with pytest.raises(CommandError, match="не найдена"):
            await plugin.page_action(other, "text", {"id": task_id, "text": "Чужая"})

        assert plugin.repo.entries(web_identity)[0]["text"] == "Моя"


class TestArchive:
    async def test_a_week_old_done_task_leaves_the_board(self, plugin, web_identity):
        plugin.repo.add(
            web_identity, "Давно", status="done",
            done_at=datetime.now() - timedelta(days=8),
        )
        plugin.repo.add(web_identity, "Вчера", status="done")

        board = await plugin.page_view(web_identity)
        archive = await plugin.page_action(web_identity, "view", {"screen": "archive"})

        assert [card["text"] for card in _cards(board, 2)] == ["Вчера"]
        assert board["tools"][0]["label"] == "Архив (1) →"
        assert [row["text"] for row in archive["list"]["rows"]] == ["Давно"]

    async def test_bringing_back_from_the_archive(self, plugin, web_identity):
        plugin.repo.add(
            web_identity, "Давно", status="done",
            done_at=datetime.now() - timedelta(days=8),
        )
        archive = await plugin.page_action(web_identity, "view", {"screen": "archive"})
        back = archive["list"]["rows"][0]["actions"][0]["params"]

        view = await plugin.page_action(
            web_identity, "move", {**archive["keep"], **back}
        )

        assert view["list"]["rows"] == []
        assert plugin.repo.entries(web_identity)[0]["done_at"] is None


class TestRepeat:
    async def _finish(self, plugin, identity, task_id):
        await plugin.page_action(identity, "move", {"id": task_id, "status": "doing"})
        await plugin.page_action(identity, "move", {"id": task_id, "status": "done"})

    async def test_finishing_makes_the_next_one_once(self, plugin, web_identity):
        due = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        await plugin.page_action(
            web_identity,
            "add",
            {"text": "Отчёт", "repeat": "weekly", "due_at": due.date().isoformat()},
        )
        task_id = str(plugin.repo.entries(web_identity)[0]["id"])

        await self._finish(plugin, web_identity, task_id)
        await plugin.page_action(
            web_identity, "move", {"id": task_id, "status": "doing"}
        )
        await plugin.page_action(
            web_identity, "move", {"id": task_id, "status": "done"}
        )

        tasks = plugin.repo.entries(web_identity)
        waiting = [task for task in tasks if task["status"] == "todo"]
        assert len(waiting) == 1
        assert waiting[0]["due_at"] == due + timedelta(weeks=1)
        assert waiting[0]["repeat"] == "weekly"

    async def test_a_late_finish_is_not_due_in_the_past(self, plugin, web_identity):
        long_ago = datetime.now() - timedelta(days=20)
        plugin.repo.add(web_identity, "Полив", due_at=long_ago)
        task_id = str(plugin.repo.entries(web_identity)[0]["id"])
        await plugin.page_action(
            web_identity, "repeat", {"id": task_id, "repeat": "daily"}
        )

        await self._finish(plugin, web_identity, task_id)

        [nxt] = [t for t in plugin.repo.entries(web_identity) if t["status"] == "todo"]
        today = _midnight(_local_now())
        assert today <= nxt["due_at"] < today + timedelta(days=1)

    def test_a_month_keeps_the_day_where_it_can(self):
        from app.plugins.todo import _after

        assert _after(datetime(2026, 1, 31), "monthly") == datetime(2026, 2, 28)
        assert _after(datetime(2026, 12, 10), "monthly") == datetime(2027, 1, 10)

    async def test_an_unknown_repeat_is_refused(self, plugin, web_identity):
        with pytest.raises(CommandError, match="повтор"):
            await plugin.page_action(
                web_identity, "add", {"text": "x", "repeat": "yearly"}
            )


async def test_the_deadline_reaches_the_date_field_as_a_date(plugin, web_identity):
    """With the time attached the browser's date field shows nothing."""
    plugin.repo.add(web_identity, "Задача", due_at=datetime(2026, 11, 8, 21, 30))

    view = await plugin.page_view(web_identity)

    assert _cards(view, 0)[0]["due_iso"] == "2026-11-08"



async def test_one_save_keeps_the_text_and_the_deadline(plugin, web_identity):
    plugin.repo.add(web_identity, "Задача")
    task_id = str(plugin.repo.entries(web_identity)[0]["id"])

    view = await plugin.page_action(
        web_identity, "text", {"id": task_id, "text": "Задача", "due_at": "2026-12-01"}
    )

    assert _cards(view, 0)[0]["due_iso"] == "2026-12-01"


async def test_deadline_worker_survives_a_failed_pass(plugin, monkeypatch, caplog):
    errors = iter([RuntimeError("pool timeout"), asyncio.CancelledError()])

    async def process_due():
        raise next(errors)

    monkeypatch.setattr(plugin, "process_due", process_due)
    monkeypatch.setattr("app.plugins.todo.POLL_INTERVAL_SECONDS", 0)

    with pytest.raises(asyncio.CancelledError):
        await plugin._worker()

    assert "Deadline reminders failed" in caplog.text


class TestTheBoardStaysCurrent:
    async def test_an_account_board_refreshes_itself(self, plugin, web_identity):
        assert (await plugin.page_view(web_identity))["live"] is True

    async def test_a_demo_board_does_not(self, plugin):
        assert (await plugin.page_view(guest("token")))["live"] is False
