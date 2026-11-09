import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from app.core.config import Config
from app.core.identity import WEB, Identity, telegram
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.reminders_repo import RemindersRepository
from app.notifications import Notifier

from .base import BasePlugin

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30
PAGE = "/reminders"
FIRED_SHOWN = timedelta(days=1)
MAX_TEXT = 500
REPEAT_LABELS = {
    "daily": "Каждый день",
    "weekdays": "По будням",
    "weekly": "Раз в неделю",
}
SNOOZES = {"10": "10 минут", "60": "Час", "tm": "До завтра"}
SNOOZE_PREFIX = "rs:"
MORNING = time(9, 0)
NIGHT_END = time(4, 0)

# One unreachable destination must not fill the journal for ever. A chat that
# was never linked is a configuration mistake, not a transient failure, and
# retrying it every half minute forever turns one missing line in .env into a
# log nobody will read. A delivery that raises is left to be retried, since
# that one may succeed; a delivery that resolves to nothing is not retried.
MAX_DROPPED_ATTEMPTS = 3


class RemindersPlugin(BasePlugin):
    name = "reminders"
    check = "/reminders"
    tables = {"reminders": "Напоминания"}
    version = "1.3.0"
    description = "Напоминания"
    page = PAGE
    page_title = "Напоминания"
    commands = {
        "/remind": "Создать напоминание",
        "/reminders": "Показать напоминания",
        "/cancel": "Отменить напоминание по номеру",
    }
    command_args = {
        "/remind": [("number", "Минуты"), ("text", "Текст")],
        "/cancel": [("number", "Номер")],
    }

    def __init__(
        self, notifier: Notifier, database: Database | None = None, persons=None
    ):
        self.repo = RemindersRepository(database)
        self._notifier = notifier
        self._persons = persons
        self._worker_task: asyncio.Task | None = None
        self._dropped_attempts: dict[int, int] = {}

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

    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        if command == "/reminders":
            return self._list(identity)

        if command == "/cancel":
            return self._cancel(args, identity)

        minutes_str, _, text = args.partition(" ")

        if not text.strip():
            raise CommandError("Usage: /remind <minutes> <text>")

        try:
            minutes = int(minutes_str)
        except ValueError:
            raise CommandError("Minutes must be a number") from None

        if minutes < 0:
            raise CommandError("Minutes must not be negative")

        if identity.kind == WEB and not self._notifier.reachable(identity):
            raise CommandError(
                "Напоминанию некуда прийти: включите уведомления на устройстве "
                "или привяжите Telegram на главной странице."
            )

        remind_at = (
            datetime.now(UTC) + timedelta(minutes=minutes)
        ).replace(tzinfo=None).isoformat()

        self.repo.add(identity, text.strip(), remind_at)

        return reply(f"Reminder set in {minutes} min")

    def _list(self, identity: Identity) -> CommandResult:
        reminders = self.repo.entries(self._readable_as(identity))

        if not reminders:
            return reply("No reminders")

        lines = ["Reminders (soonest first):"]
        for position, (_, text, remind_at) in enumerate(reminders, 1):
            lines.append(
                f"{position}. {_format_remind_at(remind_at)} — {text}"
            )

        return reply("\n".join(lines))

    def _cancel(self, args: str, identity: Identity) -> CommandResult:
        number = args.strip()

        try:
            position = int(number)
        except ValueError:
            raise CommandError("Usage: /cancel <number>") from None

        if position < 1:
            raise CommandError("Usage: /cancel <number>")

        readable = self._readable_as(identity)
        reminders = self.repo.entries(readable)

        if position > len(reminders):
            raise CommandError(
                f"No reminder {position}. Use /reminders to see {len(reminders)}.",
            )

        reminder_id = reminders[position - 1][0]

        if not self.repo.delete(readable, reminder_id):
            # Only reachable if the list changed between the two calls.
            raise CommandError(
                f"No reminder {position}. Use /reminders to see the list.",
            )

        return reply(f"Cancelled reminder {position}")

    async def _worker(self) -> None:
        last_error = None
        while True:
            try:
                await self.process_due()
            except Exception as error:
                if repr(error) != last_error:
                    logger.exception("Reminder delivery failed")
                last_error = repr(error)
            else:
                if last_error:
                    logger.warning("Reminder delivery recovered")
                last_error = None
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def process_due(self) -> None:
        """Deliver everything that has come due.

        Separate from the loop so the decision this makes — deliver, retry, or
        give up — can be exercised without a task that never returns.
        """
        for reminder_id, user_id, text, remind_at, repeat in self.repo.get_due():
            try:
                target = Identity.parse(user_id)
            except ValueError:
                logger.warning(
                    "Reminder %s has an unparsable user_id %r",
                    reminder_id,
                    user_id,
                )
                continue

            try:
                delivered = await self._notifier.deliver(
                    target,
                    text,
                    url=PAGE,
                    buttons=[
                        (label, f"{SNOOZE_PREFIX}{reminder_id}:{key}")
                        for key, label in SNOOZES.items()
                    ],
                )
            except Exception:
                # The message was never delivered, so the reminder stays due
                # and is tried again on the next pass. A Telegram outage is
                # exactly the case this is for.
                logger.exception(
                    "Failed to deliver reminder %s to %s",
                    reminder_id,
                    target,
                )
                continue

            if not delivered and not self._give_up_on(reminder_id, target):
                continue

            self.repo.mark_sent(
                reminder_id,
                next_occurrence(remind_at, repeat, datetime.now(UTC))
                if repeat
                else None,
            )
            self._dropped_attempts.pop(reminder_id, None)

    async def page_view(self, identity: Identity) -> dict:
        return self._view(self._readable_as(identity))

    async def page_action(
        self, identity: Identity, action: str, payload: dict
    ) -> dict:
        readable = self._readable_as(identity)

        if action == "add":
            self._add(identity, payload)
        elif action == "delete":
            if not self.repo.delete(readable, _reminder_id(payload)):
                raise CommandError("Напоминание не найдено.")
        elif action == "snooze":
            if payload.get("until") not in SNOOZES:
                raise CommandError("Некорректный срок.")
            if not self._snooze(readable, _reminder_id(payload), payload["until"]):
                raise CommandError("Напоминание не найдено.")
        else:
            raise CommandError(f"Неизвестное действие: {action}")

        return self._view(readable)

    def _snooze(self, readable, reminder_id: int, choice: str) -> datetime | None:
        at = snooze_until(choice, datetime.now(UTC))
        return at if self.repo.snooze(readable, reminder_id, at) else None

    def router(self) -> Router:
        router = Router(name=f"plugin:{self.name}")

        @router.callback_query(F.data.startswith(SNOOZE_PREFIX))
        async def snooze(callback: CallbackQuery) -> None:
            await callback.answer(self.snooze_from_chat(
                callback.message.chat.id if callback.message else 0,
                callback.data or "",
            ))
            if isinstance(callback.message, Message):
                with suppress(TelegramAPIError):
                    await callback.message.edit_reply_markup(reply_markup=None)

        return router

    def snooze_from_chat(self, chat_id: int, data: str) -> str:
        reminder, _, choice = data.removeprefix(SNOOZE_PREFIX).partition(":")

        if not reminder.isdigit() or choice not in SNOOZES:
            return "Некорректная кнопка."

        at = self._snooze(self._readable_as(telegram(chat_id)), int(reminder), choice)

        if at is None:
            return "Напоминание не найдено."

        return f"Отложено до {_format_remind_at(at)}"

    def _add(self, identity: Identity, payload: dict) -> None:
        text = payload.get("text", "")

        if not isinstance(text, str) or not text.strip():
            raise CommandError("Текст напоминания не может быть пустым.")

        if len(text.strip()) > MAX_TEXT:
            raise CommandError(f"Текст длиннее {MAX_TEXT} символов.")

        try:
            local = datetime.fromisoformat(str(payload.get("at", "")))
        except ValueError:
            raise CommandError("Укажите дату и время.") from None

        at = (
            local.replace(tzinfo=ZoneInfo(Config.DISPLAY_TIMEZONE))
            .astimezone(UTC)
            .replace(tzinfo=None)
        )

        if at < datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1):
            raise CommandError("Это время уже прошло.")

        repeat = payload.get("repeat") or None

        if repeat is not None and repeat not in REPEAT_LABELS:
            raise CommandError("Некорректный повтор.")

        if identity.kind == WEB and not self._notifier.reachable(identity):
            raise CommandError(
                "Напоминанию некуда прийти: включите уведомления на устройстве "
                "или привяжите Telegram на главной странице."
            )

        self.repo.add(identity, text.strip(), at, repeat)

    def _view(self, readable) -> dict:
        now = datetime.now(UTC).replace(tzinfo=None)
        entries = self.repo.page_entries(readable, now - FIRED_SHOWN)
        columns = [
            {"key": "when", "label": "Когда"},
            {"key": "text", "label": "Текст"},
            {"key": "repeat", "label": "Повтор"},
        ]

        def row(entry: dict, actions: list[dict]) -> dict:
            return {
                "when": _format_remind_at(entry["remind_at"]),
                "text": entry["text"],
                "repeat": REPEAT_LABELS.get(entry["repeat"], ""),
                "actions": actions,
            }

        def delete(entry: dict) -> dict:
            return {
                "action": "delete",
                "label": "Удалить",
                "kind": "ghost",
                "params": {"id": str(entry["id"])},
            }

        fired = [
            row(
                entry,
                [
                    {
                        "action": "snooze",
                        "label": label,
                        "kind": "ghost",
                        "params": {"id": str(entry["id"]), "until": key},
                    }
                    for key, label in SNOOZES.items()
                ]
                + [delete(entry)],
            )
            for entry in entries
            if entry["sent"]
        ]
        pending = [
            row(entry, [delete(entry)]) for entry in entries if not entry["sent"]
        ]
        lists = []

        if fired:
            lists.append(
                {"title": "Сработали за сутки", "columns": columns, "rows": fired,
                 "actions": "actions"}
            )

        lists.append(
            {"title": "Предстоящие", "columns": columns, "rows": pending,
             "actions": "actions", "empty": "Предстоящих напоминаний нет."}
        )

        zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
        default_at = (datetime.now(zone) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:00"
        )

        return {
            "title": "Напоминания",
            "layout": ["forms", "list"],
            "list": lists,
            "forms": [
                {
                    "id": "add",
                    "title": "Новое напоминание",
                    "submit": "Добавить",
                    "fields": [
                        {"name": "text", "label": "Текст", "type": "text",
                         "placeholder": "О чём напомнить", "required": True},
                        {"name": "at", "label": "Когда", "type": "datetime-local",
                         "value": default_at, "required": True},
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
                    ],
                }
            ],
        }

    def _readable_as(self, identity: Identity):
        if self._persons is None:
            return identity

        return self._persons.identities(identity)

    def _give_up_on(self, reminder_id: int, target: Identity) -> bool:
        """Count an undeliverable reminder, reporting whether to stop.

        Nobody was reached, so retrying cannot help until the account is linked
        — and trying every thirty seconds regardless is how one unconfigured
        account becomes a journal nobody reads. A delivery that raised instead
        never reaches here, because an outage is worth waiting out.
        """
        attempts = self._dropped_attempts.get(reminder_id, 0) + 1
        self._dropped_attempts[reminder_id] = attempts

        if attempts < MAX_DROPPED_ATTEMPTS:
            return False

        logger.warning(
            "Giving up on reminder %s to %s after %s attempts with nowhere "
            "to deliver it",
            reminder_id,
            target,
            attempts,
        )

        return True


def _reminder_id(payload: dict) -> int:
    raw = str(payload.get("id", ""))

    if not raw.isdigit():
        raise CommandError("Напоминание не найдено.")

    return int(raw)


def next_occurrence(remind_at: datetime, repeat: str, now: datetime) -> datetime:
    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    wall = remind_at.replace(tzinfo=UTC).astimezone(zone).replace(tzinfo=None)
    step = timedelta(weeks=1) if repeat == "weekly" else timedelta(days=1)

    while True:
        wall += step
        if repeat == "weekdays" and wall.weekday() >= 5:
            continue
        moment = wall.replace(tzinfo=zone).astimezone(UTC)
        if moment > now:
            return moment.replace(tzinfo=None)


def snooze_until(choice: str, now: datetime) -> datetime:
    if choice == "tm":
        zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
        local = now.astimezone(zone)
        day = local.date() + timedelta(days=0 if local.time() < NIGHT_END else 1)
        moment = datetime.combine(day, MORNING, tzinfo=zone).astimezone(UTC)
    else:
        moment = now + timedelta(minutes=int(choice))

    return moment.replace(tzinfo=None)


def _format_remind_at(remind_at: datetime) -> str:
    """The reminder's moment, in the reader's local time.

    ``remind_at`` comes back from the database as a naive UTC timestamp. It is
    rendered in the time zone `DISPLAY_TIMEZONE` names, not the machine's: the
    host keeps UTC regardless, and a reader comparing "soon" against the clock
    on the wall wants the wall in their own zone — for this deployment, Moscow.
    The offset is named so a bare number is never left to guess which clock it
    is on.
    """
    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    local = remind_at.replace(tzinfo=UTC).astimezone(zone)
    zone_name = local.tzname() or local.strftime("%z")

    return f"{local.strftime('%Y-%m-%d %H:%M')} {zone_name}"
