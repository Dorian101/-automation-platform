from datetime import datetime, timedelta

import pytest

from app.plugins.todo import STATUS_LABELS, TodoPlugin


class RecordingChannel:
    def __init__(self, kind: str):
        self.kind = kind
        self.received = []

    async def deliver(self, target, text):
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


class TestWorker:
    async def test_process_due_delivers_once(self, plugin):
        from app.core.identity import telegram

        sent = RecordingChannel("telegram")
        plugin._notifier._channels["telegram"] = sent

        now = datetime.now()
        # A task owned by a Telegram chat is delivered straight back to it,
        # since there is no resolver to redirect a web identity.
        plugin.repo.add(telegram(4242), "Напомнить", due_at=now + timedelta(hours=1))

        await plugin.process_due()

        assert len(sent.received) == 1
        assert "Напомнить" in sent.received[0][1]

        # A second pass finds nothing: the task is marked as notified.
        await plugin.process_due()
        assert len(sent.received) == 1
