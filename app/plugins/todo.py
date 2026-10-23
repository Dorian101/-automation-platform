"""Personal tasks, web-only, with deadline notifications.

A task is where things go before they are forgotten: a line of text, a status
in a fixed set of three, an optional deadline and an optional priority. The
page is a kanban board — three columns, one per status — because a pile that
grows should be read at a glance, not as a long vertical list.

Deadlines notify through the platform's notification layer, so a web account
paired with Telegram gets a message as the deadline approaches; an unpaired one
is simply not reminded, and the task stays in the pile.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.core.identity import Identity
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.tasks_repo import STATUSES, VALID_PRIORITIES, TasksRepository
from app.notifications import Notifier

from .base import BasePlugin

logger = logging.getLogger(__name__)

# The check is coarse on purpose: deadlines are set days ahead, not minutes, and
# an hourly pass is enough to catch one and remind once. Finer resolution would
# be a wake-up for nothing.
POLL_INTERVAL_SECONDS = 3600

# How far ahead of a deadline the owner is told. A window of a day means the
# hourly pass always lands inside it at least once before the deadline arrives.
REMIND_LEAD = timedelta(days=1)

# The statuses a task may move to from each status. A linear flow with the
# option to step back: todo -> doing -> done, plus the reverse of each move so
# a mistake is not a one-way door. The page's arrow offers only the forward
# step; the server still checks every transition against this table.
TRANSITIONS = {
    "todo": ("doing",),
    "doing": ("todo", "done"),
    "done": ("doing",),
}

# The single forward step from each status, the one the card's arrow offers.
# A terminal status has none.
NEXT_STATUS = {"todo": "doing", "doing": "done", "done": None}

# How one column is named on the page, and how a status reads inside a card.
STATUS_LABELS = {
    "todo": "К выполнению",
    "doing": "В работе",
    "done": "Сделано",
}

# How a priority reads when it is shown, and the colour the page gives it. One
# is the highest; None is a task that was not prioritised and carries no mark.
PRIORITY_LABELS = {1: "Высокий", 2: "Средний", 3: "Низкий"}


class TodoPlugin(BasePlugin):
    name = "todo"
    version = "0.1.0"
    description = "Задачи"
    web_only = True
    page = "/todo"
    page_title = "Задачи"

    # Declared so the router answers "web console only" rather than "unknown
    # command" for the commands a chat cannot express.
    commands = {
        "/task": "Add a task (web console)",
        "/tasks": "List tasks (web console)",
    }

    def __init__(
        self,
        notifier: Notifier,
        database: Database | None = None,
        persons=None,
    ):
        self.repo = TasksRepository(database)
        self._notifier = notifier
        self._persons = persons
        self._worker_task: asyncio.Task | None = None

    # -- Telegram side ------------------------------------------------------

    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        return reply("Задачи — на веб-странице.")

    # -- lifecycle ----------------------------------------------------------

    async def on_startup(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())

    async def on_shutdown(self) -> None:
        if not self._worker_task:
            return

        self._worker_task.cancel()

        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass

        self._worker_task = None

    async def _worker(self) -> None:
        while True:
            await self.process_due()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def process_due(self) -> None:
        """Remind about tasks whose deadline is inside the lead window.

        Separate from the loop so the decision — remind, and mark as reminded —
        can be exercised without a task that never returns.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        horizon = (now + REMIND_LEAD).replace(tzinfo=None)

        for task in self.repo.due_soon(now, horizon):
            try:
                target = Identity.parse(task["user_id"])
            except ValueError:
                logger.warning(
                    "Task %s has an unparsable user_id %r",
                    task["id"],
                    task["user_id"],
                )
                continue

            text = (
                f"Напоминание о задаче: {task['text']}\n"
                f"Срок: {_format_due(task['due_at'])}"
            )

            try:
                delivered = await self._notifier.deliver(target, text)
            except Exception:
                logger.exception("Failed to remind about task %s", task["id"])
                continue

            # Whether or not there was somewhere to send it, the attempt is
            # recorded so an unlinked account is not re-marked as due every hour.
            self.repo.mark_notified(task["id"])

            if not delivered:
                logger.info(
                    "Task %s has no destination; reminder dropped", task["id"]
                )

    # -- the page -----------------------------------------------------------

    async def page_view(self, identity: Identity) -> dict:
        return self._view(identity, "priority")

    async def page_action(
        self,
        identity: Identity,
        action: str,
        payload: dict,
    ) -> dict:
        order = payload.get("order") or "priority"

        if order not in ("created", "priority", "due"):
            raise CommandError("Некорректная сортировка.")

        if action == "add":
            self._add(identity, payload)
        elif action == "move":
            self._move(identity, payload)
        elif action == "priority":
            self._set_priority(identity, payload)
        elif action == "due":
            self._set_due(identity, payload)
        elif action == "delete":
            self._delete(identity, payload)
        elif action == "view":
            pass
        else:
            raise CommandError(f"Неизвестное действие: {action}")

        return self._view(identity, order)

    # -- actions ------------------------------------------------------------

    def _add(self, identity: Identity, payload: dict) -> None:
        text = payload.get("text", "").strip()

        if not text:
            raise CommandError("Текст задачи не может быть пустым.")

        priority = self._parse_priority(payload.get("priority", ""))
        due_at = self._parse_due(payload.get("due_at", ""))

        self.repo.add(identity, text, priority=priority, due_at=due_at)

    def _move(self, identity: Identity, payload: dict) -> None:
        task_id = self._task_id(payload)
        target = payload.get("status", "")

        if target not in STATUSES:
            raise CommandError("Некорректный статус.")

        task = self._find(identity, task_id)

        if task is None:
            raise CommandError("Задача не найдена.")

        if target not in TRANSITIONS.get(task["status"], ()):
            raise CommandError(
                f"Нельзя перевести из «{STATUS_LABELS[task['status']]}» "
                f"в «{STATUS_LABELS[target]}»."
            )

        self.repo.set_status(identity, task_id, target)

    def _set_priority(self, identity: Identity, payload: dict) -> None:
        task_id = self._task_id(payload)
        priority = self._parse_priority(payload.get("priority", ""))

        if not self.repo.set_priority(identity, task_id, priority):
            raise CommandError("Задача не найдена.")

    def _set_due(self, identity: Identity, payload: dict) -> None:
        task_id = self._task_id(payload)
        due_at = self._parse_due(payload.get("due_at", ""))

        if not self.repo.set_due(identity, task_id, due_at):
            raise CommandError("Задача не найдена.")

    def _delete(self, identity: Identity, payload: dict) -> None:
        task_id = self._task_id(payload)

        if not self.repo.delete(identity, task_id):
            raise CommandError("Задача не найдена.")

    # -- helpers ------------------------------------------------------------

    def _task_id(self, payload: dict) -> int:
        raw = payload.get("id", "")

        if not str(raw).isdigit():
            raise CommandError("Задача не найдена.")

        return int(raw)

    def _parse_priority(self, raw: str) -> int | None:
        value = (raw or "").strip()

        if not value:
            return None

        if not value.isdigit() or int(value) not in VALID_PRIORITIES:
            raise CommandError("Приоритет — число от 1 до 3.")

        return int(value)

    def _parse_due(self, raw: str) -> datetime | None:
        value = (raw or "").strip()

        if not value:
            return None

        try:
            return datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            raise CommandError("Срок — дата в формате ГГГГ-ММ-ДД.") from None

    def _find(self, identity: Identity, task_id: int) -> dict | None:
        for task in self.repo.entries(self._readable_as(identity)):
            if task["id"] == task_id:
                return task

        return None

    # -- what the page draws ------------------------------------------------

    def _view(self, identity: Identity, order: str) -> dict:
        readable = self._readable_as(identity)
        tasks = self.repo.entries(readable, order=order)

        now = datetime.now(UTC).replace(tzinfo=None)

        columns = []
        for status in STATUSES:
            columns.append(
                {
                    "status": status,
                    "label": STATUS_LABELS[status],
                    "cards": [
                        self._card(task, status, now, order) for task in tasks
                        if task["status"] == status
                    ],
                }
            )

        return {
            "title": "Задачи",
            "order": order,
            "kanban": {"columns": columns},
            "forms": self._forms(),
        }

    def _card(self, task: dict, status: str, now: datetime, order: str) -> dict:
        due = task["due_at"]
        overdue = due is not None and due < now and status != "done"

        return {
            "id": task["id"],
            "text": task["text"],
            "status": status,
            "priority": task["priority"],
            "priority_label": PRIORITY_LABELS.get(task["priority"]),
            "due_at": _format_due(due) if due else None,
            "due_iso": due.isoformat() if due else None,
            "created_at": task["created_at"].strftime("%d.%m.%Y"),
            "overdue": overdue,
            "moves": self._forward_move(status),
        }

    def _forward_move(self, status: str) -> list[dict]:
        """The card's arrow: the one step forward, or nothing at the end.

        The server's TRANSITIONS still allow a step back, but the card offers
        only the forward move, because a board read left-to-right moves a card
        that way and a backwards step is an edit, not a glance.
        """
        target = NEXT_STATUS.get(status)

        if target is None:
            return []

        return [{"status": target, "label": STATUS_LABELS[target]}]

    def _forms(self) -> list[dict]:
        return [
            {
                "id": "add",
                "title": "Новая задача",
                "submit": "Добавить",
                "dialog": True,
                "fields": [
                    {
                        "name": "text",
                        "label": "Текст",
                        "type": "text",
                        "placeholder": "Что нужно сделать",
                        "required": True,
                    },
                    {
                        "name": "priority",
                        "label": "Приоритет",
                        "type": "select",
                        "options": [
                            {"value": "1", "label": "Высокий"},
                            {"value": "2", "label": "Средний"},
                            {"value": "3", "label": "Низкий"},
                        ],
                    },
                    {
                        "name": "due_at",
                        "label": "Срок",
                        "type": "date",
                        "placeholder": "ГГГГ-ММ-ДД",
                    },
                ],
            }
        ]

    def _readable_as(self, identity: Identity):
        if self._persons is None:
            return identity

        return self._persons.identities(identity)


def _format_due(due_at: datetime) -> str:
    """A deadline as a plain date.

    A task is due on a day, not at a minute, so there is no time and no zone
    to display: the value is read as the day it is, and reading it back later
    must not shift it by an offset that was never meant to be part of it.
    """
    return due_at.strftime("%d.%m.%Y").strip()
