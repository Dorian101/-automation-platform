from datetime import datetime, timedelta

import pytest

from app.core.identity import WEB, Identity
from app.db.tasks_repo import TasksRepository

OTHER = Identity(kind=WEB, id="someone-else")


@pytest.fixture
def repo(database_with_schema):
    return TasksRepository(database_with_schema)


def _soon():
    return datetime.now() + timedelta(hours=2)


class TestTasks:
    def test_adds_and_reads_back(self, repo, web_identity):
        repo.add(web_identity, "Купить молоко", priority=1, due_at=_soon())

        listed = repo.entries(web_identity)

        assert listed[0]["text"] == "Купить молоко"
        assert listed[0]["status"] == "todo"
        assert listed[0]["priority"] == 1
        assert listed[0]["due_at"] is not None
        assert listed[0]["created_at"] is not None

    def test_defaults_are_todo_and_no_priority(self, repo, web_identity):
        repo.add(web_identity, "Просто задача")

        listed = repo.entries(web_identity)

        assert listed[0]["status"] == "todo"
        assert listed[0]["priority"] is None
        assert listed[0]["due_at"] is None

    def test_two_accounts_never_see_each_other(self, repo, web_identity):
        repo.add(web_identity, "Моя")
        repo.add(OTHER, "Чужая")

        assert [t["text"] for t in repo.entries(web_identity)] == ["Моя"]
        assert [t["text"] for t in repo.entries(OTHER)] == ["Чужая"]

    def test_status_transition_is_scoped_to_the_account(self, repo, web_identity):
        repo.add(web_identity, "Моя")
        repo.add(OTHER, "Чужая")
        mine = repo.entries(web_identity)[0]["id"]
        theirs = repo.entries(OTHER)[0]["id"]

        assert repo.set_status(web_identity, theirs, "doing") is False
        assert repo.set_status(web_identity, mine, "doing") is True

    def test_delete_is_scoped_to_the_account(self, repo, web_identity):
        repo.add(web_identity, "Моя")
        mine = repo.entries(web_identity)[0]["id"]

        assert repo.delete(OTHER, mine) is False
        assert repo.delete(web_identity, mine) is True

    def test_priority_accepts_only_the_three_levels(self, repo, web_identity):
        repo.add(web_identity, "Задача", priority=3)

        listed = repo.entries(web_identity)

        assert listed[0]["priority"] == 3


class TestSorting:
    def test_priority_order_puts_high_first_and_none_last(
        self, repo, web_identity
    ):
        repo.add(web_identity, "Обычная")
        repo.add(web_identity, "Важная", priority=1)
        repo.add(web_identity, "Низкая", priority=3)

        listed = repo.entries(web_identity, order="priority")

        assert [t["text"] for t in listed] == ["Важная", "Низкая", "Обычная"]

    def test_due_order_puts_soonest_first_and_none_last(
        self, repo, web_identity
    ):
        repo.add(web_identity, "Без срока")
        repo.add(web_identity, "Позже", due_at=datetime.now() + timedelta(days=5))
        repo.add(web_identity, "Скоро", due_at=datetime.now() + timedelta(days=1))

        listed = repo.entries(web_identity, order="due")

        assert [t["text"] for t in listed] == ["Скоро", "Позже", "Без срока"]

    def test_status_filter_narrows(self, repo, web_identity):
        repo.add(web_identity, "Активная")
        repo.add(web_identity, "Сделанная")
        done = [
            t for t in repo.entries(web_identity) if t["text"] == "Сделанная"
        ][0]["id"]
        repo.set_status(web_identity, done, "done")

        assert [t["text"] for t in repo.entries(web_identity, status="done")] == [
            "Сделанная"
        ]


class TestDueSoon:
    def test_returns_tasks_inside_the_horizon(self, repo, web_identity):
        now = datetime.now()
        repo.add(web_identity, "Скоро", due_at=now + timedelta(hours=1))

        due = repo.due_soon(now, now + timedelta(days=1))

        assert [t["text"] for t in due] == ["Скоро"]

    def test_skips_already_done_and_already_notified(self, repo, web_identity):
        now = datetime.now()
        repo.add(web_identity, "Сделанная", due_at=now + timedelta(hours=1))
        done = repo.entries(web_identity)[0]["id"]
        repo.set_status(web_identity, done, "done")

        repo.add(web_identity, "Напомненная", due_at=now + timedelta(hours=1))
        reminded = [
            t for t in repo.entries(web_identity) if t["text"] == "Напомненная"
        ][0]["id"]
        repo.mark_notified(reminded)

        repo.add(web_identity, "Ждёт напоминания", due_at=now + timedelta(hours=1))

        due = repo.due_soon(now, now + timedelta(days=1))

        assert [t["text"] for t in due] == ["Ждёт напоминания"]

    def test_mark_notified_sets_the_flag(self, repo, web_identity):
        now = datetime.now()
        repo.add(web_identity, "Задача", due_at=now + timedelta(hours=1))
        task_id = repo.entries(web_identity)[0]["id"]

        repo.mark_notified(task_id)

        assert repo.due_soon(now, now + timedelta(days=1)) == []
