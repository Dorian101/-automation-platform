"""Personal spending.

Web-only, with a page of its own. Both halves of that are deliberate and
worth stating, because they are the first plugin to use either.

The page exists because a spending analysis is not a conversation. Asking for
it through commands means typing ``/spend 320 еда``, and asking for last month
means a different command again — the interaction is composed of strings on
the way in, which is exactly what a form exists to stop.

Web-only because an analytics screen has no meaning in a chat and the bot has
no form to draw it on. Declaring it here keeps the router from ever handing a
plugin a message it cannot answer, rather than the plugin checking the
transport for itself.

Money is Decimal from end to end. The column is NUMERIC and Python is not
allowed to reinterpret it as a float on the way through.
"""

import re
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.identity import Identity, is_guest
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.expenses_repo import ExpensesRepository

from .base import BasePlugin

# A guest on the demonstration page is capped so a page on the open internet
# cannot grow without limit. The account holder is not capped: this is a
# personal tool and the limit exists for exposure, not for tidiness.
GUEST_CATEGORY_LIMIT = 12
GUEST_EXPENSE_LIMIT = 100
GUEST_INCOME_LIMIT = 30

#: What an income can be. Salary is told apart because the month ends with two
#: results: salary less spending, and everything received less spending.
INCOME_KINDS = {"salary": "Зарплата", "other": "Пополнение"}

#: Two screens of one page: entering and editing, and the month's analytics
#: with its list of operations. Each has its own order of sections.
LAYOUTS = {
    "entry": ["totals", "tools", "forms", "list"],
    "analytics": ["totals", "tools", "forms", "chart", "table", "list"],
}

MAX_CATEGORY_NAME = 40

#: The first amount NUMERIC(12, 2) cannot store.
MAX_AMOUNT = Decimal("10000000000")

# A month reaches the plugin as a string in a request body. `YYYY-MM` is the
# only shape accepted, and it is checked before the value reaches the query
# rather than by the database refusing to parse it.
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _is_month(value: str) -> bool:
    return isinstance(value, str) and MONTH_PATTERN.match(value) is not None


def _month_label(month: str) -> str:
    """``"2026-05"`` into ``"май 2026"``, so the page is readable at a glance.

    Parsed rather than sliced: a label built from the string would read
    ``"май"`` for a month number outside the twelve and put a wrong name on a
    real one.
    """
    names = (
        "январь",
        "февраль",
        "март",
        "апрель",
        "май",
        "июнь",
        "июль",
        "август",
        "сентябрь",
        "октябрь",
        "ноябрь",
        "декабрь",
    )

    year, _, number = month.partition("-")

    try:
        index = int(number) - 1
    except ValueError:
        return month

    if not 0 <= index < len(names):
        return month

    return f"{names[index]} {year}"


def _shift_month(month: str, delta: int) -> str:
    """The month ``delta`` steps from ``month``, negative included."""
    year, _, number = month.partition("-")

    try:
        total = int(year) * 12 + (int(number) - 1) + delta
    except ValueError:
        return month

    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _id(payload: dict, key: str, missing: str) -> int:
    """A row id from the page, refused unless it is a plain number."""
    value = payload.get(key, "")

    if not isinstance(value, str) or not value.isdigit():
        raise CommandError(missing)

    return int(value)


def _amount(text) -> Decimal:
    """A positive amount as typed: spaces and a decimal comma allowed."""
    try:
        value = Decimal(text.replace(" ", "").replace(",", "."))
    except (InvalidOperation, AttributeError):
        raise CommandError("Сумма должна быть числом.") from None

    if not value.is_finite() or value <= 0:
        raise CommandError("Сумма должна быть больше нуля.")

    # What NUMERIC(12, 2) holds: a third decimal would be rounded away, to
    # zero for "0,004", and twelve digits overflow the column.
    if value != value.quantize(Decimal("0.01")):
        raise CommandError("Не больше двух знаков после запятой.")

    if value >= MAX_AMOUNT:
        raise CommandError("Слишком большая сумма.")

    return value


def _note(payload: dict) -> str:
    note = payload.get("note") or ""

    if not isinstance(note, str):
        raise CommandError("Заметка должна быть текстом.")

    return note


def _day(text) -> date:
    """A date as the form sends it; today when left blank."""
    try:
        return date.fromisoformat(text) if text else date.today()
    except (ValueError, TypeError):
        raise CommandError("Дата должна быть в формате ГГГГ-ММ-ДД.") from None


def _category_name(payload: dict) -> str:
    name = payload.get("name", "")
    name = name.strip() if isinstance(name, str) else ""

    if not name:
        raise CommandError("Название категории не может быть пустым.")

    if len(name) > MAX_CATEGORY_NAME:
        raise CommandError(f"Название длиннее {MAX_CATEGORY_NAME} символов.")

    return name


@contextmanager
def _taken_name(name: str):
    """Word the unique index's refusal of an active category name.

    The partial unique index is the rule, not the plugin: any other writer has
    to obey it too. Its own message names a constraint in a form nobody can
    act on, so the wording is ours.
    """
    try:
        yield
    except Exception as error:
        if "idx_expense_categories_active" in str(error):
            raise CommandError(f"Категория «{name}» уже есть.") from None
        raise


def _money(value: Decimal) -> str:
    """Format an amount for the page.

    Rounded to whole roubles with no decimals, because a spending breakdown is
    read at a glance and the cents are noise. The rounding happens on output
    only: nothing stored has been rounded, so the monthly totals still add up
    to what was actually spent.
    """
    return f"{value.quantize(Decimal('1')):,.0f}".replace(",", " ").replace("-", "−")


def _spend_note(value: str = "") -> dict:
    return {
        "name": "note",
        "label": "Заметка",
        "type": "text",
        "placeholder": "Где или на что",
        "value": value,
    }


def _percent(value) -> str:
    return f"{value:.1f}".replace(".", ",") + "%"


#: How a month's total splits into separate purchases in the demonstration, in
#: percent. One line a category reads as a summary rather than as a month lived.
DEMO_PURCHASES = {
    "Продукты": (31, 24, 19, 15, 11),
    "Кафе и рестораны": (52, 29, 19),
    "Транспорт": (40, 35, 25),
}


#: The demonstration's monthly salary, a little under what the months spend.
DEMO_SALARY = 110_000


def _purchases(total: int, shares: tuple[int, ...]) -> list[int]:
    """Split a total by percent; the last part takes what rounding left."""
    parts = [total * share // 100 for share in shares[:-1]]

    return [*parts, total - sum(parts)]


class ExpensesPlugin(BasePlugin):
    name = "expenses"
    tables = {
        "expenses": "Расходы",
        "expense_categories": "Категории расходов",
        "incomes": "Пополнения",
    }
    version = "0.1.0"
    description = "Личные расходы"
    web_only = True
    demo = True
    page = "/expenses"
    page_title = "Расходы"

    # Declared so the router can recognise them and answer "web console only"
    # in a chat instead of "unknown command" — a command this plugin refuses
    # for a reason is a different thing from one that does not exist.
    commands = {
        "/spend": "Record a spending entry (web console)",
        "/spending": "Show the spending breakdown (web console)",
    }

    automation_events = {
        "expense.added": ("Трата внесена", ["text", "amount", "category"]),
    }
    automation_actions = {
        "expense.add": (
            "Внести трату",
            [("category", "Категория"), ("amount", "Сумма"), ("note", "Заметка")],
        ),
    }

    def __init__(self, database: Database | None = None, persons=None):
        self.repo = ExpensesRepository(database)
        self._persons = persons

    # -- Telegram side ------------------------------------------------------

    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        """Answer in a chat.

        Declared ``web_only``, so the router refuses before this is reached.
        It exists because the contract requires the method, not because a
        chat can do anything useful here.
        """
        return reply("Spending is on the web console.")

    # -- the page -----------------------------------------------------------

    async def page_view(self, identity: Identity) -> dict:
        month = date.today().strftime("%Y-%m")
        return self._view(identity, month)

    async def page_action(
        self,
        identity: Identity,
        action: str,
        payload: dict,
    ) -> dict:
        month = payload.get("month") or date.today().strftime("%Y-%m")

        if not _is_month(month):
            # The page is not a trusted caller and the month arrives as a
            # string in a request body. Rejecting a malformed one here is what
            # keeps a bad value out of the SQL, and what turns a 500 into a
            # message somebody can act on.
            raise CommandError("Некорректный месяц.")

        payload = {**payload, "month": month}

        # An edit form or a category filter is part of what the next view
        # shows, not something stored: the page asks for it each time.
        editing = None
        category = None

        if action == "spend":
            await self._announce(identity, self._spend(identity, payload), ())
        elif action == "spendsave":
            self._save_spend(identity, payload)
        elif action == "income":
            self._add_income(identity, payload)
        elif action == "incomesave":
            self._save_income(identity, payload)
        elif action == "catadd":
            self._add_category(identity, payload)
        elif action == "catarchive":
            self._archive_category(identity, payload)
        elif action == "catsave":
            self._rename_category(identity, payload)
        elif action == "delete":
            self._delete(identity, payload)
        elif action == "incomedelete":
            self.repo.delete_income(
                self._readable_as(identity),
                _id(payload, "income_id", "Пополнение не найдено."),
            )
        elif action == "edit":
            editing = self._editing_spend(identity, payload)
        elif action == "incomeedit":
            editing = self._editing_income(identity, payload)
        elif action == "catrename":
            editing = self._editing_category(identity, payload)
        elif action == "filter":
            category = _id(payload, "category_id", "Категория не найдена.")
        elif action == "month":
            # Changes nothing in storage, only which month the page is
            # reporting. Declared as an action rather than hidden in the page
            # so the month is a parameter of the same request that draws it.
            pass
        else:
            raise CommandError(f"Неизвестное действие: {action}")

        screen = payload.get("screen")

        if screen not in LAYOUTS:
            screen = "entry"

        # A filter set earlier comes back through `keep`, so editing or
        # deleting a row of a filtered list does not drop the filter.
        kept = payload.get("category")

        if category is None and isinstance(kept, str) and kept.isdigit():
            category = int(kept)

        return self._view(identity, month, editing, category, screen)

    # -- what the page draws ------------------------------------------------

    def _view(
        self,
        identity: Identity,
        month: str,
        editing: dict | None = None,
        category: int | None = None,
        screen: str = "entry",
    ) -> dict:
        """Assemble everything the page shows for one month.

        The shape below is the vocabulary: the web layer knows how to paint a
        chart, a form and a table, and knows nothing about spending. Adding a
        section here needs no change anywhere else.

        ``editing`` is a ready edit form that takes the place of the others;
        ``category`` narrows the month's list to one category. ``screen`` is
        ``entry`` (totals, forms, categories) or ``analytics`` (totals, chart,
        table, the month's operations); ``keep`` makes the page send it back
        with every action, so the month arrows do not leave the screen.
        """
        readable = self._readable_as(identity)

        rows = self.repo.by_category(readable, month)
        total = sum((row["total"] for row in rows), Decimal("0"))
        previous_month = _shift_month(month, -1)
        previous = self.repo.monthly_total(readable, previous_month)

        incomes = self.repo.incomes(readable, month)
        salary = sum(
            (one["amount"] for one in incomes if one["kind"] == "salary"),
            Decimal("0"),
        )
        received = sum((one["amount"] for one in incomes), Decimal("0"))

        categories = self.repo.categories(readable)

        analytics = screen == "analytics"
        view = {
            "title": "Расходы",
            "layout": LAYOUTS[screen],
            "keep": {
                "screen": screen,
                "category": str(category) if category and analytics else "",
            },
            "tools": [
                {
                    "action": "month",
                    "label": "← К вводу" if analytics else "Аналитика и операции →",
                    "params": {
                        "month": month,
                        "screen": "entry" if analytics else "analytics",
                    },
                }
            ],
            "month": {
                "value": month,
                "label": _month_label(month),
                "available": self.repo.available_months(readable),
                # The neighbours are computed here rather than in the browser.
                # The arithmetic has to survive a year boundary, and a second
                # implementation of it is a second chance to get it wrong —
                # which is what happened: the page did year * 12 without
                # dividing back, and September 2026 came out as 24320.
                "prev": _shift_month(month, -1),
                "next": _shift_month(month, 1),
            },
            "totals": {
                # A list of figures with their own captions rather than named
                # fields like `current` and `previous`. The page draws what it
                # is given, so a second plugin with a different set of figures
                # does not need a second shape, and this one did not get to
                # decide what the other looked like.
                "items": [
                    {
                        "value": _money(total),
                        "caption": f"Расходы за {_month_label(month).lower()}",
                    },
                    *(
                        [
                            {
                                "value": _money(previous),
                                "caption": "За прошлый месяц",
                            }
                        ]
                        if previous > 0
                        else []
                    ),
                    # Two results, so a month that ends in the black only
                    # because somebody sent money does not read as saving.
                    {
                        "value": _money(salary - total),
                        "caption": "Зарплата − траты",
                    },
                    {
                        "value": _money(received - total),
                        "caption": "Все пополнения − траты",
                    },
                ],
                "delta": self._change(total, previous),
            },
        }

        if analytics:
            view["chart"] = self._chart(rows, total)
            view["table"] = self._table(rows, total, month)
            view["list"] = [
                self._operations(readable, month, incomes, categories, category)
            ]
            view["forms"] = [editing] if editing else []
        else:
            view["list"] = [self._categories(categories, month)]
            view["forms"] = [editing] if editing else self._forms(categories, month)

        return view

    def _change(self, total: Decimal, previous: Decimal) -> dict | None:
        """How this month compares with the last one.

        Returned as text plus a direction rather than a formatted string, so
        the page decides the colour and does not have to read the sign out of
        it. Nothing here says whether more or less is good: that depends on a
        budget this tool does not hold, and a report that editorialises it is
        reporting something it was not asked.
        """
        if previous <= 0:
            return None

        delta = (total - previous) / previous * 100
        sign = "+" if delta >= 0 else "−"

        return {
            "text": f"{sign}{_percent(abs(delta))} к прошлому месяцу",
            "direction": "up" if delta >= 0 else "down",
        }

    def _chart(self, rows: list[dict], total: Decimal) -> dict:
        """A bar per category, sized against the largest one.

        The scale is relative rather than against the total, because a month
        where one category dominates would otherwise render every other bar as
        an invisible sliver. The share is still on each bar as a figure, so
        the relative picture and the absolute one are both available.
        """
        if not rows:
            return {"kind": "bars", "items": [], "empty": "В этом месяце трат нет"}

        largest = max(row["total"] for row in rows)

        return {
            "kind": "bars",
            "items": [
                {
                    "label": row["category"],
                    "value": _money(row["total"]),
                    "count": row["count"],
                    # A hundredths fraction, so the width is decided by data
                    # rather than by how many digits a formatted string had.
                    "share": float(row["total"] / largest * 100),
                    "share_text": (
                        f"{row['total'] / total * 100:.0f}%" if total > 0 else "—"
                    ),
                }
                for row in rows
            ],
        }

    def _table(self, rows: list[dict], total: Decimal, month: str) -> dict:
        """The same numbers in figures, under the chart.

        A bar shows the shape of a month; it cannot be read to the last digit.
        Both views come from one aggregation so they cannot disagree.
        """
        return {
            "columns": [
                {"key": "category", "label": "Категория"},
                {"key": "count", "label": "Трат", "num": True},
                {"key": "total", "label": "Сумма", "num": True},
                {"key": "share", "label": "Доля", "num": True},
            ],
            "rows": [
                {
                    "category": row["category"],
                    "count": row["count"],
                    "total": _money(row["total"]),
                    "share": _percent(row["total"] / total * 100) if total > 0 else "—",
                    "actions": [
                        {
                            "action": "filter",
                            "label": "Траты",
                            "kind": "ghost",
                            "params": {
                                "category_id": str(row["category_id"]),
                                "month": month,
                            },
                        }
                    ],
                }
                for row in rows
            ],
            "actions": "actions",
        }

    def _forms(self, categories: list[dict], month: str) -> list[dict]:
        """The two forms the page offers, with the categories read live.

        The options come from the repository rather than from anything written
        down here, so a category created through any other path is in this list
        the moment it exists.
        """
        options = [
            {"value": str(category["id"]), "label": category["name"]}
            for category in categories
        ]

        forms = [
            {
                "id": "spend",
                "title": "Добавить трату",
                "submit": "Добавить",
                "fields": [
                    {
                        "name": "category_id",
                        "label": "Категория",
                        "type": "select",
                        "options": options,
                        # Said here rather than left to the page: the list is
                        # empty because this account has created no category
                        # yet, and that is a fact about the data, not about the
                        # interface.
                        "empty": (
                            "Сначала добавьте категорию"
                            if not options
                            else ""
                        ),
                        "required": True,
                    },
                    {
                        "name": "amount",
                        "label": "Сумма",
                        "type": "text",
                        "placeholder": "320",
                        "required": True,
                    },
                    {
                        "name": "spent_at",
                        "label": "Дата",
                        "type": "date",
                        # Follows the month on screen, so recording a cost
                        # against September does not land in August.
                        "value": self._default_date(month),
                        # Kept when the form is redrawn. The month sets it;
                        # a person who changes it afterwards has decided
                        # something the month cannot know, and a redraw that
                        # undid that would discard the decision.
                        "sticky": True,
                    },
                    _spend_note(),
                ],
            },
            {
                "id": "income",
                "title": "Добавить пополнение",
                "submit": "Добавить",
                "fields": self._income_fields(month),
            },
            {
                "id": "catadd",
                "title": "Новая категория",
                "submit": "Добавить",
                "fields": [
                    {
                        "name": "name",
                        "label": "Название",
                        "type": "text",
                        "placeholder": "Продукты",
                        "required": True,
                    }
                ],
            },
        ]

        # A first visit has nothing to spend against, so the form that fixes
        # that comes first rather than under one that cannot be submitted.
        return forms if options else [forms[-1], *forms[:-1]]

    def _income_fields(self, month: str, income: dict | None = None) -> list[dict]:
        """The income form's fields, filled from ``income`` when editing."""
        return [
            {
                "name": "kind",
                "label": "Тип",
                "type": "select",
                "options": [
                    {"value": key, "label": label}
                    for key, label in INCOME_KINDS.items()
                ],
                "value": income["kind"] if income else "salary",
            },
            {
                "name": "amount",
                "label": "Сумма",
                "type": "text",
                "placeholder": "50000",
                "required": True,
                **({"value": str(income["amount"])} if income else {}),
            },
            {
                "name": "received_at",
                "label": "Дата",
                "type": "date",
                "value": (
                    income["received_at"].isoformat()
                    if income
                    else self._default_date(month)
                ),
                "sticky": income is None,
            },
            {
                "name": "note",
                "label": "Заметка",
                "type": "text",
                "placeholder": "От кого или за что",
                **({"value": income["note"] or ""} if income else {}),
            },
        ]

    def _check_guest_room(
        self, identity: Identity, expenses: bool = False, incomes: bool = False
    ) -> None:
        """Refuse a write that would take a guest past its cap.

        Capped because the demonstration page is on the open internet and its
        namespace is the only one an anonymous visitor can write to. The cap is
        about exposure, not tidiness, so it applies to guests only — the
        account holder is not capped anywhere in this plugin.

        Counted rather than remembered: a counter in the process would reset
        on every restart, which is exactly when a cap is still needed.
        """
        if not is_guest(identity):
            return

        if expenses:
            written = self.repo.count(identity)
            limit = GUEST_EXPENSE_LIMIT
            what = "трат"
        elif incomes:
            written = self.repo.income_count(identity)
            limit = GUEST_INCOME_LIMIT
            what = "пополнений"
        else:
            written = len(self.repo.categories(self._readable_as(identity)))
            limit = GUEST_CATEGORY_LIMIT
            what = "категорий"

        if written >= limit:
            raise CommandError(
                f"Демо ограничено {limit} {what}. Данные очищаются ежедневно.",
            )

    def _default_date(self, month: str) -> str:
        """The date the form opens on, for the month currently on screen.

        Today while the current month is in view — a person recording today's
        expense should not have to rewind the form to the first of the month —
        and the first of the month for any other, so the offered date is always
        inside the month being looked at. Anything else would file the expense
        into a month the reader is not looking at.
        """
        today = date.today()

        if month == today.strftime("%Y-%m"):
            return today.isoformat()

        return f"{month}-01"

    def _operations(
        self,
        readable,
        month: str,
        incomes: list[dict],
        categories: list[dict],
        category: int | None,
    ) -> dict:
        """The month's spending and incomes in one list, newest first.

        Narrowed to one category, it holds that category's spending only, and
        a button brings the whole month back.
        """
        expenses = self.repo.recent(
            readable, limit=None, month=month, category_id=category
        )
        label = _month_label(month)
        rows = [
            {
                "date": entry["spent_at"],
                "id": entry["id"],
                "spent_at": entry["spent_at"].strftime("%d.%m.%Y"),
                "category": entry["category"],
                "note": entry["note"] or "",
                "amount": _money(entry["amount"]),
                "actions": self._row_actions(
                    "edit", "delete", "expense_id", entry["id"], month
                ),
            }
            for entry in expenses
        ]

        if category is None:
            rows += [
                {
                    "date": income["received_at"],
                    "id": income["id"],
                    "spent_at": income["received_at"].strftime("%d.%m.%Y"),
                    "category": INCOME_KINDS[income["kind"]],
                    "note": income["note"] or "",
                    "amount": "+" + _money(income["amount"]),
                    "actions": self._row_actions(
                        "incomeedit", "incomedelete", "income_id", income["id"], month
                    ),
                }
                for income in incomes
            ]
            title = f"Операции за {label}"
            tools = []
        else:
            name = next(
                (one["name"] for one in categories if one["id"] == category),
                "архивная категория",
            )
            title = f"Траты: {name}, {label}"
            tools = [
                {
                    "action": "month",
                    "label": "Показать все",
                    "params": {"month": month, "category": ""},
                }
            ]

        rows.sort(key=lambda row: (row["date"], row["id"]), reverse=True)

        for row in rows:
            del row["date"], row["id"]

        return {
            "title": title,
            "tools": tools,
            "empty": "В этом месяце операций нет",
            "columns": [
                {"key": "spent_at", "label": "Дата"},
                {"key": "category", "label": "Категория"},
                {"key": "note", "label": "Заметка"},
                {"key": "amount", "label": "Сумма", "num": True},
            ],
            "rows": rows,
            "actions": "actions",
        }

    def _row_actions(
        self, edit: str, delete: str, key: str, row_id: int, month: str
    ) -> list[dict]:
        """Edit and delete for one row of the month's list."""
        params = {key: str(row_id), "month": month}

        return [
            {"action": edit, "label": "Изменить", "kind": "ghost", "params": params},
            {"action": delete, "label": "Удалить", "kind": "ghost", "params": params},
        ]

    def _categories(self, categories: list[dict], month: str) -> dict:
        """The active categories, to rename or to retire.

        Retiring hides a category from the form and keeps what was spent in
        it, so the months already recorded do not change.
        """
        return {
            "title": "Категории",
            "columns": [
                {"key": "name", "label": "Название"},
                {"key": "count", "label": "Трат всего", "num": True},
            ],
            "rows": [
                {
                    "name": category["name"],
                    "count": category["expenses_count"],
                    "actions": [
                        {
                            "action": "catrename",
                            "label": "Переименовать",
                            "kind": "ghost",
                            "params": params,
                        },
                        {
                            "action": "catarchive",
                            "label": "Удалить",
                            "kind": "ghost",
                            "params": params,
                        },
                    ],
                }
                for category in categories
                for params in [{"category_id": str(category["id"]), "month": month}]
            ],
            "actions": "actions",
        }

    # -- edit forms ---------------------------------------------------------

    def _edit_form(
        self, form_id: str, title: str, fields: list[dict], hidden: dict, month: str
    ) -> dict:
        """A form that changes one row, with a way back to the month."""
        return {
            "id": form_id,
            "title": title,
            "submit": "Сохранить",
            "focus": True,
            "fields": [
                *fields,
                *(
                    {"name": name, "type": "hidden", "value": value}
                    for name, value in {**hidden, "month": month}.items()
                ),
            ],
            "cancel": {
                "label": "Отмена",
                "action": "month",
                "params": {"month": month},
            },
        }

    def _editing_spend(self, identity: Identity, payload: dict) -> dict:
        expense = self.repo.get(
            self._readable_as(identity), _id(payload, "expense_id", "Трата не найдена.")
        )

        if expense is None:
            raise CommandError("Трата не найдена.")

        options = [
            {"value": str(one["id"]), "label": one["name"]}
            for one in self.repo.categories(self._readable_as(identity))
        ]

        # A retired category stays selectable for the entry already in it, or
        # saving any other change would quietly move the entry elsewhere.
        if str(expense["category_id"]) not in [one["value"] for one in options]:
            options.append(
                {"value": str(expense["category_id"]), "label": expense["category"]}
            )

        return self._edit_form(
            "spendsave",
            "Изменить трату",
            [
                {
                    "name": "category_id",
                    "label": "Категория",
                    "type": "select",
                    "options": options,
                    "value": str(expense["category_id"]),
                    "required": True,
                },
                {
                    "name": "amount",
                    "label": "Сумма",
                    "type": "text",
                    "value": str(expense["amount"]),
                    "required": True,
                },
                {
                    "name": "spent_at",
                    "label": "Дата",
                    "type": "date",
                    "value": expense["spent_at"].isoformat(),
                },
                _spend_note(expense["note"] or ""),
            ],
            {"expense_id": str(expense["id"])},
            payload.get("month", ""),
        )

    def _editing_income(self, identity: Identity, payload: dict) -> dict:
        income = self.repo.get_income(
            self._readable_as(identity),
            _id(payload, "income_id", "Пополнение не найдено."),
        )

        if income is None:
            raise CommandError("Пополнение не найдено.")

        return self._edit_form(
            "incomesave",
            "Изменить пополнение",
            self._income_fields(payload.get("month", ""), income),
            {"income_id": str(income["id"])},
            payload.get("month", ""),
        )

    def _editing_category(self, identity: Identity, payload: dict) -> dict:
        category_id = _id(payload, "category_id", "Категория не найдена.")
        category = next(
            (
                one
                for one in self.repo.categories(self._readable_as(identity))
                if one["id"] == category_id
            ),
            None,
        )

        if category is None:
            raise CommandError("Категория не найдена.")

        return self._edit_form(
            "catsave",
            "Переименовать категорию",
            [
                {
                    "name": "name",
                    "label": "Название",
                    "type": "text",
                    "value": category["name"],
                    "required": True,
                }
            ],
            {"category_id": str(category_id)},
            payload.get("month", ""),
        )

    # -- actions ------------------------------------------------------------

    async def automation(
        self, identity: Identity, action: str, params: dict, chain: tuple
    ) -> str:
        if action != "expense.add":
            return await super().automation(identity, action, params, chain)

        wanted = str(params.get("category", "")).strip().lower()
        category = next(
            (
                one
                for one in self.repo.categories(self._readable_as(identity))
                if one["name"].lower() == wanted
            ),
            None,
        )

        if category is None:
            raise CommandError(f"Категория «{params.get('category', '')}» не найдена.")

        fields = (
            category["id"],
            _amount(str(params.get("amount", ""))),
            date.today(),
            str(params.get("note", "")),
        )
        self._check_guest_room(identity, expenses=True)
        self.repo.add(identity, *fields)
        await self._announce(identity, fields, chain)

        return f"Внесена трата {_money(fields[1])} в «{category['name']}»"

    async def _announce(self, identity: Identity, fields: tuple, chain: tuple) -> None:
        category_id, amount, _, note = fields
        name = next(
            (
                one["name"]
                for one in self.repo.categories(self._readable_as(identity))
                if one["id"] == category_id
            ),
            "",
        )
        await self.publish(
            "expense.added",
            identity,
            {"text": note, "amount": amount, "category": name},
            chain,
        )

    def _spend(self, identity: Identity, payload: dict) -> tuple:
        self._check_guest_room(identity, expenses=True)
        fields = self._expense_fields(payload)

        try:
            self.repo.add(identity, *fields)
        except ValueError:
            # The repository refuses a category that is not this account's —
            # the id is a global sequence. Its message is about storage; the
            # person on the page only needs to know the category is not theirs.
            raise CommandError("Категория не найдена.") from None

        return fields

    def _save_spend(self, identity: Identity, payload: dict) -> None:
        expense_id = _id(payload, "expense_id", "Трата не найдена.")

        if not self.repo.update(
            self._readable_as(identity), expense_id, *self._expense_fields(payload)
        ):
            raise CommandError("Трата или категория не найдена.")

    def _expense_fields(self, payload: dict) -> tuple:
        """Category, amount, date and note of a spending form, checked."""
        category_id = payload.get("category_id", "")

        if not category_id:
            raise CommandError("Выберите категорию.")

        if not isinstance(category_id, str) or not category_id.isdigit():
            raise CommandError("Категория не найдена.")

        return (
            int(category_id),
            _amount(payload.get("amount", "")),
            _day(payload.get("spent_at", "")),
            _note(payload),
        )

    def _add_income(self, identity: Identity, payload: dict) -> None:
        self._check_guest_room(identity, incomes=True)
        self.repo.add_income(identity, *self._income_values(payload))

    def _save_income(self, identity: Identity, payload: dict) -> None:
        income_id = _id(payload, "income_id", "Пополнение не найдено.")

        if not self.repo.update_income(
            self._readable_as(identity), income_id, *self._income_values(payload)
        ):
            raise CommandError("Пополнение не найдено.")

    def _income_values(self, payload: dict) -> tuple:
        """Kind, amount, date and note of an income form, checked."""
        kind = payload.get("kind", "")

        if kind not in INCOME_KINDS:
            raise CommandError("Выберите тип пополнения.")

        return (
            kind,
            _amount(payload.get("amount", "")),
            _day(payload.get("received_at", "")),
            _note(payload),
        )

    def _add_category(self, identity: Identity, payload: dict) -> None:
        name = _category_name(payload)
        self._check_guest_room(identity)

        # A category removed by mistake comes back with its spending when its
        # name is added again, instead of a second one splitting the reports.
        with _taken_name(name):
            if not self.repo.restore_category(self._readable_as(identity), name):
                self.repo.add_category(self._readable_as(identity), name)

    def _rename_category(self, identity: Identity, payload: dict) -> None:
        category_id = _id(payload, "category_id", "Категория не найдена.")
        name = _category_name(payload)

        with _taken_name(name):
            renamed = self.repo.rename_category(
                self._readable_as(identity), category_id, name
            )

        if not renamed:
            raise CommandError("Категория не найдена.")

    def _archive_category(self, identity: Identity, payload: dict) -> None:
        category_id = payload.get("category_id", "")

        if not category_id.isdigit():
            raise CommandError("Категория не найдена.")

        if not self.repo.archive_category(
            self._readable_as(identity), int(category_id)
        ):
            raise CommandError("Категория не найдена.")

    def _delete(self, identity: Identity, payload: dict) -> None:
        expense_id = payload.get("expense_id", "")

        if not expense_id.isdigit():
            raise CommandError("Трата не найдена.")

        self.repo.delete(self._readable_as(identity), int(expense_id))

    # -- demonstration ------------------------------------------------------

    async def seed_demo(self, identity: Identity) -> None:
        """Three months of invented spending, for the demonstration page.

        Every figure here is made up. The categories are the ordinary ones and
        the amounts are the kind a person actually spends, because a
        demonstration made of round numbers gives away that nobody used it.

        The current month is seeded on today's day and the two before it on a
        fixed one, so nothing is ever dated ahead of today: an expense dated in
        the future would sit in the list counting days that have not happened,
        and the visitor would have to work out why the demo was wrong before
        they could believe the rest of it.
        """
        today = date.today()
        amounts = {
            "Продукты": (18_400, 21_300, 17_900),
            "Кафе и рестораны": (7_600, 9_200, 6_800),
            "Транспорт": (12_000, 14_500, 11_200),
            "Жильё": (65_000, 65_000, 65_000),
            "Коммунальные": (8_400, 7_100, 9_600),
            "Подписки": (2_990, 2_990, 2_990),
        }

        for back in range(3):
            month = _shift_month(today.strftime("%Y-%m"), -back)
            year, _, number = month.partition("-")
            day = today.day if back == 0 else min(15, int(number))

            for name, values in amounts.items():
                for index, amount in enumerate(
                    _purchases(values[back], DEMO_PURCHASES.get(name, (100,)))
                ):
                    self.repo.add(
                        identity,
                        self._category_id(identity, name),
                        Decimal(amount),
                        date(int(year), int(number), max(1, day - index * 3)),
                    )

            # Salary that covers most of a month, and once a month in three
            # money from home, so the two results on the page differ.
            self.repo.add_income(
                identity,
                "salary",
                Decimal(DEMO_SALARY),
                date(int(year), int(number), min(day, 5)),
            )

            if back == 1:
                self.repo.add_income(
                    identity,
                    "other",
                    Decimal(15_000),
                    date(int(year), int(number), day),
                    "Помощь от родителей",
                )

    def _category_id(self, identity: Identity, name: str) -> int:
        """Find the demo category, creating it the first time.

        Looked up rather than held, because the namespace is empty when the
        seed runs and there is nothing to hold the ids in.
        """
        for category in self.repo.categories(self._readable_as(identity)):
            if category["name"] == name:
                return category["id"]

        return self.repo.add_category(self._readable_as(identity), name)

    # -- shared -------------------------------------------------------------
