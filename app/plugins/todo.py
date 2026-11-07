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
import calendar
import logging
from datetime import UTC, datetime, timedelta

from app.core.identity import Identity
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.tasks_repo import REPEATS, STATUSES, VALID_PRIORITIES, TasksRepository
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

# The steps a card offers: one forward and one back, both within TRANSITIONS.
NEXT_STATUS = {"todo": "doing", "doing": "done", "done": None}
PREVIOUS_STATUS = {"todo": None, "doing": "todo", "done": "doing"}

# How a repeat reads on the page.
REPEAT_LABELS = {
    "daily": "Каждый день",
    "weekly": "Каждую неделю",
    "monthly": "Каждый месяц",
}

# How long a finished task stays on the board before it goes to the archive.
DONE_ON_BOARD = timedelta(days=7)

MAX_TEXT = 200
MAX_DESCRIPTION = 2000

# The board and the archive of finished tasks, two screens of one page.
LAYOUTS = {"board": ["tools", "kanban", "forms"], "archive": ["tools", "list"]}

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
    tables = {"tasks": "Задачи"}
    version = "0.1.0"
    description = "Список дел на канбан-доске с приоритетами и дедлайнами"
    web_only = True
    page = "/todo"
    page_title = "Задачи"
    demo = True

    # Declared so the router answers "web console only" rather than "unknown
    # command" for the commands a chat cannot express.
    commands = {
        "/task": "Add a task (web console)",
        "/tasks": "List tasks (web console)",
    }

    automation_events = {
        "task.created": ("Задача создана", ["text"]),
        "task.done": ("Задача закрыта", ["text"]),
    }
    automation_actions = {
        "task.create": (
            "Создать задачу",
            [("text", "Текст задачи"), ("due_days", "Срок через N дней")],
        ),
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
            try:
                await self.process_due()
            except Exception:
                logger.exception("Deadline reminders failed")
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

    async def seed_demo(self, identity: Identity) -> None:
        """A small board of invented tasks, for the demonstration page.

        Every task here is made up. They span the three columns so a visitor
        sees the board working rather than one empty pile, they carry a mix of
        priorities, and one deadline is deliberately yesterday so the overdue
        marker is what gets shown, not what gets described. Nothing is dated
        ahead of tomorrow: a task due a week out would be indistinguishable
        from one that was never due at all.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        yesterday = now - timedelta(days=1)
        tomorrow = now + timedelta(days=1)
        in_two_days = now + timedelta(days=2)

        demo = [
            ("Подготовить отчёт по расходам", "todo", 1, tomorrow),
            ("Позвонить по поводу поставки", "todo", None, in_two_days),
            ("Обновить резюме", "todo", 3, None),
            ("Разобрать письма за неделю", "doing", 2, yesterday),
            ("Обновить платформу на VPS", "doing", 1, None),
            ("Собрать демо-данные для гостей", "done", None, yesterday),
            ("Найти потерянный документ", "done", 2, yesterday),
        ]

        for text, status, priority, due_at in demo:
            self.repo.add(
                identity, text, priority=priority, due_at=due_at, status=status
            )

        # A card with details, a repeating one, and finished work old enough
        # for the archive, so each feature has something to show.
        self.repo.add(
            identity,
            "Оплатить коммунальные",
            priority=2,
            due_at=in_two_days,
            description="Свет, вода, интернет. Квитанции в почте.",
            repeat="monthly",
        )
        self.repo.add(
            identity,
            "Сдать показания счётчиков",
            status="done",
            done_at=now - timedelta(days=12),
        )

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

        screen = payload.get("screen")

        if screen not in LAYOUTS:
            screen = "board"

        if action == "add":
            text = self._add(identity, payload)
            await self.publish("task.created", identity, {"text": text})
        elif action == "move":
            task = self._move(identity, payload)
            if payload.get("status") == "done":
                await self.publish("task.done", identity, {"text": task["text"]})
        elif action == "priority":
            self._set_priority(identity, payload)
        elif action == "due":
            self._set_due(identity, payload)
        elif action == "text":
            self._set_text(identity, payload)
        elif action == "repeat":
            self._set_repeat(identity, payload)
        elif action == "delete":
            self._delete(identity, payload)
        elif action == "view":
            pass
        else:
            raise CommandError(f"Неизвестное действие: {action}")

        return self._view(identity, order, screen)

    # -- actions ------------------------------------------------------------

    async def automation(
        self, identity: Identity, action: str, params: dict, chain: tuple
    ) -> str:
        if action != "task.create":
            return await super().automation(identity, action, params, chain)

        text, _ = _text_and_description({"text": params.get("text", "")})
        days = str(params.get("due_days", "")).strip()

        if days and not days.isdigit():
            raise CommandError("Срок — число дней.")

        due_at = None

        if days:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            due_at = today + timedelta(days=int(days))

        self.repo.add(identity, text, due_at=due_at)
        await self.publish("task.created", identity, {"text": text}, chain)

        return f"Создана задача «{text}»"

    def _add(self, identity: Identity, payload: dict) -> str:
        text, description = _text_and_description(payload)
        priority = self._parse_priority(payload.get("priority", ""))
        due_at = self._parse_due(payload.get("due_at", ""))

        self.repo.add(
            identity,
            text,
            priority=priority,
            due_at=due_at,
            description=description,
            repeat=_parse_repeat(payload.get("repeat", "")),
        )

        return text

    def _move(self, identity: Identity, payload: dict) -> dict:
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

        if target == "done":
            self._next_of(identity, task)

        return task

    def _next_of(self, identity: Identity, task: dict) -> None:
        """Put the next one of a repeating task on the board, once.

        Due one period after this one, or after today for a task with no
        deadline, and never in the past: a weekly task finished a fortnight
        late comes back next week, not two weeks ago.
        """
        if not task["repeat"] or task["repeated"]:
            return

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        due = _after(task["due_at"] or today, task["repeat"])

        while due < today:
            due = _after(due, task["repeat"])

        self.repo.add(
            identity,
            task["text"],
            priority=task["priority"],
            due_at=due,
            description=task["description"],
            repeat=task["repeat"],
        )
        self.repo.mark_repeated(task["id"])

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

    def _set_text(self, identity: Identity, payload: dict) -> None:
        task_id = self._task_id(payload)
        text, description = _text_and_description(payload)
        # The card saves its deadline with the text, under one button; checked
        # first, so a bad date saves nothing.
        has_due = "due_at" in payload
        due_at = self._parse_due(payload.get("due_at", "")) if has_due else None

        if not self.repo.set_text(identity, task_id, text, description):
            raise CommandError("Задача не найдена.")

        if has_due:
            self.repo.set_due(identity, task_id, due_at)

    def _set_repeat(self, identity: Identity, payload: dict) -> None:
        task_id = self._task_id(payload)
        repeat = _parse_repeat(payload.get("repeat", ""))

        if not self.repo.set_repeat(identity, task_id, repeat):
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

    def _view(self, identity: Identity, order: str, screen: str = "board") -> dict:
        readable = self._readable_as(identity)
        tasks = self.repo.entries(readable, order=order)

        now = datetime.now(UTC).replace(tzinfo=None)
        shown_since = datetime.now() - DONE_ON_BOARD
        archived = [
            task
            for task in tasks
            if task["status"] == "done"
            and task["done_at"] is not None
            and task["done_at"] < shown_since
        ]
        archived_ids = {task["id"] for task in archived}

        view = {
            "title": "Задачи",
            "order": order,
            "layout": LAYOUTS[screen],
            "keep": {"screen": screen, "order": order},
        }

        if screen == "archive":
            view["tools"] = [
                {"action": "view", "label": "← К доске", "params": {"screen": "board"}}
            ]
            view["list"] = self._archive(archived)
            return view

        columns = []
        for status in STATUSES:
            columns.append(
                {
                    "status": status,
                    "label": STATUS_LABELS[status],
                    "cards": [
                        self._card(task, status, now, order) for task in tasks
                        if task["status"] == status and task["id"] not in archived_ids
                    ],
                }
            )

        view["tools"] = [
            {
                "action": "view",
                "label": f"Архив ({len(archived)}) →",
                "params": {"screen": "archive"},
            }
        ]
        view["kanban"] = {"columns": columns}
        view["forms"] = self._forms()
        return view

    def _archive(self, tasks: list[dict]) -> dict:
        """Finished tasks older than a week, newest first, to bring back or remove."""
        tasks = sorted(tasks, key=lambda task: task["done_at"], reverse=True)

        return {
            "title": "Архив",
            "empty": "Архив пуст: сюда уходят задачи, сделанные больше недели назад.",
            "columns": [
                {"key": "text", "label": "Задача"},
                {"key": "done_at", "label": "Сделано"},
            ],
            "rows": [
                {
                    "text": task["text"],
                    "done_at": task["done_at"].strftime("%d.%m.%Y"),
                    "actions": [
                        {
                            "action": "move",
                            "label": "Вернуть в работу",
                            "kind": "ghost",
                            "params": {"id": str(task["id"]), "status": "doing"},
                        },
                        {
                            "action": "delete",
                            "label": "Удалить",
                            "kind": "ghost",
                            "params": {"id": str(task["id"])},
                        },
                    ],
                }
                for task in tasks
            ],
            "actions": "actions",
        }

    def _card(self, task: dict, status: str, now: datetime, order: str) -> dict:
        due = task["due_at"]
        overdue = due is not None and due < now and status != "done"

        return {
            "id": task["id"],
            "text": task["text"],
            "description": task["description"] or "",
            "status": status,
            "priority": task["priority"],
            "priority_label": PRIORITY_LABELS.get(task["priority"]),
            "due_at": _format_due(due) if due else None,
            # A date field takes the date alone; with the time it shows empty.
            "due_iso": due.date().isoformat() if due else None,
            "repeat": task["repeat"] or "",
            "repeat_label": REPEAT_LABELS.get(task["repeat"]),
            "created_at": task["created_at"].strftime("%d.%m.%Y"),
            "overdue": overdue,
            "moves": self._moves(status),
        }

    def _moves(self, status: str) -> list[dict]:
        """The card's arrows: a step back and a step forward, where there is one."""
        return [
            {"status": target, "label": STATUS_LABELS[target], "dir": direction}
            for direction, target in (
                ("back", PREVIOUS_STATUS[status]),
                ("forward", NEXT_STATUS[status]),
            )
            if target is not None
        ]

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
                    {
                        "name": "repeat",
                        "label": "Повтор",
                        "type": "select",
                        "options": [
                            {"value": "", "label": "Не повторять"},
                            *(
                                {"value": key, "label": label}
                                for key, label in REPEAT_LABELS.items()
                            ),
                        ],
                    },
                    {
                        "name": "description",
                        "label": "Описание",
                        "type": "text",
                        "placeholder": "Детали, ссылки",
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


def _text_and_description(payload: dict) -> tuple[str, str | None]:
    """A task's line and its details from the page, checked."""
    text = payload.get("text", "")
    description = payload.get("description") or ""

    if not isinstance(text, str) or not isinstance(description, str):
        raise CommandError("Текст задачи должен быть строкой.")

    text = text.strip()
    description = description.strip()

    if not text:
        raise CommandError("Текст задачи не может быть пустым.")

    if len(text) > MAX_TEXT:
        raise CommandError(f"Текст задачи длиннее {MAX_TEXT} символов.")

    if len(description) > MAX_DESCRIPTION:
        raise CommandError(f"Описание длиннее {MAX_DESCRIPTION} символов.")

    return text, description or None


def _parse_repeat(raw) -> str | None:
    if not raw:
        return None

    if raw not in REPEATS:
        raise CommandError("Некорректный повтор.")

    return raw


def _after(moment: datetime, repeat: str) -> datetime:
    """One period on from ``moment``; a month keeps the day where it can."""
    if repeat == "daily":
        return moment + timedelta(days=1)

    if repeat == "weekly":
        return moment + timedelta(weeks=1)

    year = moment.year + moment.month // 12
    month = moment.month % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])

    return moment.replace(year=year, month=month, day=day)
