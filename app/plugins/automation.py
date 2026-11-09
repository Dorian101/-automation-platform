import asyncio
import calendar
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.core.config import Config
from app.core.events import MAX_CHAIN, Event
from app.core.identity import Identity, is_guest
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.rules_repo import RulesRepository

from .base import BasePlugin

logger = logging.getLogger(__name__)

SCHEDULE = "schedule"
POLL_SECONDS = 300
GUEST_RULE_LIMIT = 10
MAX_NAME = 60
MAX_PARAM = 500

PLACEHOLDERS = {"текст": "text", "сумма": "amount", "категория": "category"}
PERIODS = {"daily": "Каждый день", "weekly": "Каждую неделю", "monthly": "Каждый месяц"}
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
LAYOUTS = {"rules": ["tools", "forms", "list"], "journal": ["tools", "list"]}


def _id(payload: dict, key: str) -> int:
    value = payload.get(key, "")

    if not isinstance(value, str) or not value.isdigit():
        raise CommandError("Правило не найдено.")

    return int(value)


def _text(payload: dict, key: str) -> str:
    value = payload.get(key) or ""

    if not isinstance(value, str):
        raise CommandError("Поля правила должны быть текстом.")

    value = value.strip()

    if len(value) > MAX_PARAM:
        raise CommandError(f"Поле длиннее {MAX_PARAM} символов.")

    return value


def _fill(template: str, data: dict) -> str:
    for word, key in PLACEHOLDERS.items():
        value = data.get(key, "")
        if isinstance(value, Decimal):
            value = f"{value.quantize(Decimal('1')):,.0f}".replace(",", " ")
        template = template.replace("{" + word + "}", str(value))

    return template


def _matches(rule: dict, data: dict) -> bool:
    if rule["condition_text"]:
        haystack = f"{data.get('text', '')} {data.get('category', '')}".lower()
        if rule["condition_text"].lower() not in haystack:
            return False

    if rule["condition_amount"] is not None:
        amount = data.get("amount")
        if amount is None or Decimal(amount) <= rule["condition_amount"]:
            return False

    return True


def _due(schedule: dict, local: datetime) -> bool:
    period = schedule.get("period")
    day = int(schedule.get("day") or 1)

    if local.hour < int(schedule.get("hour", 9)):
        return False

    if period == "weekly":
        return local.isoweekday() == day

    if period == "monthly":
        last = calendar.monthrange(local.year, local.month)[1]
        return local.day == min(day, last)

    return period == "daily"


def _local(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC).astimezone(ZoneInfo(Config.DISPLAY_TIMEZONE))


class AutomationPlugin(BasePlugin):
    name = "automation"
    tables = {"rule_runs": "Срабатывания правил", "rules": "Правила"}
    version = "0.1.0"
    description = "Правила «если — то» между плагинами"
    web_only = True
    page = "/automation"
    page_title = "Автоматизации"
    demo = True

    automation_actions = {
        "telegram.send": ("Отправить уведомление", [("text", "Текст сообщения")]),
    }

    def __init__(
        self, manager, notifier, database: Database | None = None, persons=None
    ):
        self.repo = RulesRepository(database)
        self._manager = manager
        self._notifier = notifier
        self._persons = persons
        self._worker_task: asyncio.Task | None = None
        manager.events.subscribe(self.handle)

    async def execute(
        self, command: str, args: str, identity: Identity
    ) -> CommandResult:
        return reply("Автоматизации — на веб-странице.")

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
                await self.process_schedule(datetime.now(UTC))
            except Exception:
                logger.exception("Scheduled rules failed")
            await asyncio.sleep(POLL_SECONDS)

    def _switched_on(self) -> bool:
        return self._manager.find_by_page(self.page) is self

    async def handle(self, event: Event) -> None:
        if event.name == SCHEDULE or not self._switched_on():
            return

        for rule in self.repo.listening(self._readable_as(event.identity), event.name):
            if _matches(rule, event.data):
                await self._fire(rule, event)

    async def process_schedule(self, now: datetime) -> None:
        if not self._switched_on():
            return

        local = now.astimezone(ZoneInfo(Config.DISPLAY_TIMEZONE))

        for rule in self.repo.scheduled():
            if rule["last_fired_on"] == local.date() or not _due(
                rule["schedule"], local
            ):
                continue

            self.repo.mark_fired(rule["id"], local.date())
            await self._fire(rule, Event(SCHEDULE, Identity.parse(rule["user_id"])))

    async def _fire(self, rule: dict, event: Event) -> None:
        if rule["id"] in event.chain or len(event.chain) >= MAX_CHAIN:
            self.repo.log(
                rule, False, "Пропущено: правило замкнуло бы цепочку на себя."
            )
            return

        plugin = self._manager.find_action(rule["action"])

        if plugin is None:
            self.repo.log(rule, False, "Действие сейчас недоступно: плагин выключен.")
            return

        params = {
            key: _fill(str(value), event.data) for key, value in rule["params"].items()
        }

        try:
            message = await plugin.automation(
                Identity.parse(rule["user_id"]),
                rule["action"],
                params,
                event.chain + (rule["id"],),
            )
        except CommandError as error:
            self.repo.log(rule, False, str(error))
            return
        except Exception:
            logger.exception("Rule %s failed", rule["id"])
            self.repo.log(rule, False, "Внутренняя ошибка.")
            return

        self.repo.log(rule, True, message)
        owner = Identity.parse(rule["user_id"])
        plugin.changed(owner)
        self.changed(owner)

    async def automation(
        self, identity: Identity, action: str, params: dict, chain: tuple
    ) -> str:
        if action != "telegram.send":
            return await super().automation(identity, action, params, chain)

        text = str(params.get("text", "")).strip()

        if not text:
            raise CommandError("Пустое сообщение.")

        if not await self._notifier.deliver(identity, text):
            raise CommandError(
                "Некуда отправить: нет устройств с уведомлениями и Telegram."
            )

        return "Уведомление отправлено."

    async def seed_demo(self, identity: Identity) -> None:
        self.repo.add(
            identity,
            "Крупная трата → задача",
            "expense.added",
            "task.create",
            {
                "text": "Проверить крупную трату: {сумма} ₽, {категория}",
                "due_days": "1",
            },
            condition_amount=Decimal("5000"),
        )
        self.repo.add(
            identity,
            "Оплатил коммуналку → трата",
            "task.done",
            "expense.add",
            {"category": "Коммунальные", "amount": "8400", "note": "{текст}"},
            condition_text="коммунал",
        )
        self.repo.add(
            identity,
            "Разбор почты по понедельникам",
            SCHEDULE,
            "task.create",
            {"text": "Разобрать почту за неделю", "due_days": "0"},
            schedule={"period": "weekly", "day": 1, "hour": 10},
        )

    async def page_view(self, identity: Identity) -> dict:
        return self._view(identity, "rules", None)

    async def page_action(self, identity: Identity, action: str, payload: dict) -> dict:
        screen = payload.get("screen")

        if screen not in LAYOUTS:
            screen = "rules"

        draft = None
        readable = self._readable_as(identity)

        if action == "draft":
            draft = self._step_two(payload)
        elif action == "create":
            self._create(identity, payload)
        elif action == "toggle":
            if not self.repo.set_enabled(
                readable, _id(payload, "rule_id"), payload.get("enabled") == "1"
            ):
                raise CommandError("Правило не найдено.")
        elif action == "delete":
            if not self.repo.delete(readable, _id(payload, "rule_id")):
                raise CommandError("Правило не найдено.")
        elif action != "view":
            raise CommandError(f"Неизвестное действие: {action}")

        return self._view(identity, screen, draft)

    def _events(self) -> dict:
        return {
            **self._manager.automation_events(),
            SCHEDULE: ("По расписанию", []),
        }

    def _checked_basics(self, payload: dict) -> tuple[str, str, str]:
        name = _text(payload, "name")
        trigger = _text(payload, "trigger")
        action = _text(payload, "action")

        if not name:
            raise CommandError("Дайте правилу название.")

        if len(name) > MAX_NAME:
            raise CommandError(f"Название длиннее {MAX_NAME} символов.")

        if trigger not in self._events():
            raise CommandError("Выберите, когда срабатывает правило.")

        if action not in self._manager.automation_actions():
            raise CommandError("Выберите, что делает правило.")

        return name, trigger, action

    def _create(self, identity: Identity, payload: dict) -> None:
        name, trigger, action = self._checked_basics(payload)

        if is_guest(identity) and len(self.repo.entries(identity)) >= GUEST_RULE_LIMIT:
            raise CommandError(
                f"Демо ограничено {GUEST_RULE_LIMIT} правилами. "
                "Данные очищаются ежедневно."
            )

        schedule = None
        condition_text = None
        condition_amount = None

        if trigger == SCHEDULE:
            schedule = self._checked_schedule(payload)
        else:
            condition_text = _text(payload, "condition_text") or None
            raw = _text(payload, "condition_amount").replace(" ", "").replace(",", ".")
            if raw:
                try:
                    condition_amount = Decimal(raw)
                except InvalidOperation:
                    raise CommandError("Сумма в условии должна быть числом.") from None
                if not condition_amount.is_finite() or condition_amount < 0:
                    raise CommandError("Сумма в условии должна быть числом.")

        fields = self._manager.automation_actions()[action][1]
        params = {key: _text(payload, key) for key, _ in fields}

        if not params[fields[0][0]]:
            raise CommandError(f"Заполните поле «{fields[0][1]}».")

        self.repo.add(
            identity,
            name,
            trigger,
            action,
            params,
            schedule=schedule,
            condition_text=condition_text,
            condition_amount=condition_amount,
        )

    def _checked_schedule(self, payload: dict) -> dict:
        period = _text(payload, "period")
        day = _text(payload, "day") or "1"
        hour = _text(payload, "hour")

        if period not in PERIODS:
            raise CommandError("Выберите период.")

        if not hour.isdigit() or not 0 <= int(hour) <= 23:
            raise CommandError("Час — число от 0 до 23.")

        limit = {"daily": 31, "weekly": 7, "monthly": 31}[period]

        if not day.isdigit() or not 1 <= int(day) <= limit:
            raise CommandError(
                "День недели — число от 1 (пн) до 7 (вс)."
                if period == "weekly"
                else "День месяца — число от 1 до 31."
            )

        return {"period": period, "day": int(day), "hour": int(hour)}

    def _step_two(self, payload: dict) -> dict:
        name, trigger, action = self._checked_basics(payload)
        events = self._events()
        label, fields = self._manager.automation_actions()[action]
        hint = ", ".join(
            "{" + word + "}"
            for word, key in PLACEHOLDERS.items()
            if key in events[trigger][1]
        )

        if trigger == SCHEDULE:
            when = [
                {
                    "name": "period",
                    "label": "Период",
                    "type": "select",
                    "options": [{"value": k, "label": v} for k, v in PERIODS.items()],
                    "value": "weekly",
                },
                {
                    "name": "day",
                    "label": "День",
                    "type": "text",
                    "placeholder": "1–7 для недели (1 — пн), 1–31 для месяца",
                },
                {
                    "name": "hour",
                    "label": "Час",
                    "type": "select",
                    "options": [
                        {"value": str(h), "label": f"{h:02d}:00"} for h in range(24)
                    ],
                    "value": "9",
                },
            ]
        else:
            when = [
                {
                    "name": "condition_text",
                    "label": "Если текст содержит",
                    "type": "text",
                    "placeholder": "необязательно",
                }
            ]
            if "amount" in events[trigger][1]:
                when.append(
                    {
                        "name": "condition_amount",
                        "label": "Если сумма больше",
                        "type": "text",
                        "placeholder": "необязательно",
                    }
                )

        what = [
            {
                "name": key,
                "label": field_label,
                "type": "text",
                "placeholder": f"можно {hint}" if hint else "",
                "required": index == 0,
            }
            for index, (key, field_label) in enumerate(fields)
        ]

        return {
            "id": "create",
            "title": f"«{name}»: {events[trigger][0]} → {label}",
            "submit": "Создать правило",
            "focus": True,
            "fields": [
                *when,
                *what,
                *(
                    {"name": key, "type": "hidden", "value": value}
                    for key, value in {
                        "name": name,
                        "trigger": trigger,
                        "action": action,
                    }.items()
                ),
            ],
            "cancel": {"label": "Отмена", "action": "view", "params": {}},
        }

    def _step_one(self) -> dict:
        return {
            "id": "draft",
            "title": "Новое правило",
            "submit": "Далее",
            "fields": [
                {
                    "name": "name",
                    "label": "Название",
                    "type": "text",
                    "placeholder": "Крупная трата → задача",
                    "required": True,
                },
                {
                    "name": "trigger",
                    "label": "Когда",
                    "type": "select",
                    "options": [
                        {"value": key, "label": label}
                        for key, (label, _) in self._events().items()
                    ],
                },
                {
                    "name": "action",
                    "label": "Сделать",
                    "type": "select",
                    "options": [
                        {"value": key, "label": label}
                        for key, (
                            label,
                            _,
                        ) in self._manager.automation_actions().items()
                    ],
                },
            ],
        }

    def _view(self, identity: Identity, screen: str, draft: dict | None) -> dict:
        readable = self._readable_as(identity)
        view = {
            "title": "Автоматизации",
            "layout": LAYOUTS[screen],
            "keep": {"screen": screen},
        }

        if screen == "journal":
            view["tools"] = [
                {
                    "action": "view",
                    "label": "← К правилам",
                    "params": {"screen": "rules"},
                }
            ]
            view["list"] = self._journal(readable)
            return view

        view["tools"] = [
            {"action": "view", "label": "Журнал →", "params": {"screen": "journal"}}
        ]
        view["forms"] = [draft] if draft else [self._step_one()]
        view["list"] = self._rules(readable)
        return view

    def _rules(self, readable) -> dict:
        events = self._events()
        actions = self._manager.automation_actions()
        counts = self.repo.counts(readable)
        rows = []

        for rule in self.repo.entries(readable):
            count, last = counts.get(rule["id"], (0, None))
            params = rule["params"]
            action_label, fields = actions.get(rule["action"], (rule["action"], []))
            first = " · ".join(str(params[key]) for key, _ in fields if params.get(key))
            params_key = {"rule_id": str(rule["id"])}
            rows.append(
                {
                    "name": rule["name"] + ("" if rule["enabled"] else " (выключено)"),
                    "when": self._when(rule, events),
                    "condition": self._condition(rule),
                    "what": f"{action_label}: {first}" if first else action_label,
                    "fired": (
                        f"{count} раз, последний {_local(last).strftime('%d.%m %H:%M')}"
                        if count
                        else "ещё нет"
                    ),
                    "actions": [
                        {
                            "action": "toggle",
                            "label": "Выключить" if rule["enabled"] else "Включить",
                            "kind": "ghost",
                            "params": {
                                **params_key,
                                "enabled": "0" if rule["enabled"] else "1",
                            },
                        },
                        {
                            "action": "delete",
                            "label": "Удалить",
                            "kind": "ghost",
                            "params": params_key,
                        },
                    ],
                }
            )

        return {
            "title": "Правила",
            "empty": "Правил пока нет: соберите первое из блоков выше.",
            "columns": [
                {"key": "name", "label": "Название"},
                {"key": "when", "label": "Когда"},
                {"key": "condition", "label": "Если"},
                {"key": "what", "label": "Сделать"},
                {"key": "fired", "label": "Сработало"},
            ],
            "rows": rows,
            "actions": "actions",
        }

    def _when(self, rule: dict, events: dict) -> str:
        if rule["trigger"] != SCHEDULE:
            return events.get(rule["trigger"], (rule["trigger"], []))[0]

        schedule = rule["schedule"]
        period = schedule.get("period")
        hour = f"{int(schedule.get('hour', 9)):02d}:00"
        day = int(schedule.get("day") or 1)

        if period == "weekly":
            return f"{PERIODS[period]}, {WEEKDAYS[day - 1]}, {hour}"

        if period == "monthly":
            return f"{PERIODS[period]}, {day}-го, {hour}"

        return f"{PERIODS.get(period, period)}, {hour}"

    def _condition(self, rule: dict) -> str:
        parts = []

        if rule["condition_text"]:
            parts.append(f"текст содержит «{rule['condition_text']}»")

        if rule["condition_amount"] is not None:
            amount = f"{rule['condition_amount'].quantize(Decimal('1')):,.0f}"
            parts.append(f"сумма больше {amount.replace(',', ' ')}")

        return "; ".join(parts) or "—"

    def _journal(self, readable) -> dict:
        return {
            "title": "Журнал срабатываний",
            "empty": "Пока ничего не срабатывало.",
            "columns": [
                {"key": "at", "label": "Когда"},
                {"key": "rule", "label": "Правило"},
                {"key": "result", "label": "Итог"},
                {"key": "message", "label": "Что произошло"},
            ],
            "rows": [
                {
                    "at": _local(run["at"]).strftime("%d.%m %H:%M"),
                    "rule": run["rule"],
                    "result": "✓" if run["ok"] else "✗",
                    "message": run["message"],
                }
                for run in self.repo.runs(readable)
            ],
        }

    def _readable_as(self, identity: Identity):
        if self._persons is None:
            return identity

        return self._persons.identities(identity)
