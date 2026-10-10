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

from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.identity import Identity
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.expenses_repo import ExpensesRepository

from .base import BasePlugin

# A guest on the demonstration page is capped so a page on the open internet
# cannot grow without limit. The account holder is not capped: this is a
# personal tool and the limit exists for exposure, not for tidiness.
GUEST_CATEGORY_LIMIT = 12
GUEST_EXPENSE_LIMIT = 100

MAX_CATEGORY_NAME = 40


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


def _money(value: Decimal) -> str:
    """Format an amount for the page.

    Rounded to whole roubles with no decimals, because a spending breakdown is
    read at a glance and the cents are noise. The rounding happens on output
    only: nothing stored has been rounded, so the monthly totals still add up
    to what was actually spent.
    """
    return f"{value.quantize(Decimal('1')):,.0f}".replace(",", " ")


class ExpensesPlugin(BasePlugin):
    name = "expenses"
    version = "0.1.0"
    description = "Track personal spending"
    web_only = True
    page = "/expenses"
    page_title = "Расходы"

    # Declared so the router can recognise them and answer "web console only"
    # in a chat instead of "unknown command" — a command this plugin refuses
    # for a reason is a different thing from one that does not exist.
    commands = {
        "/spend": "Record a spending entry (web console)",
        "/spending": "Show the spending breakdown (web console)",
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

        if action == "spend":
            self._spend(identity, payload)
        elif action == "catadd":
            self._add_category(identity, payload)
        elif action == "catarchive":
            self._archive_category(identity, payload)
        elif action == "delete":
            self._delete(identity, payload)
        elif action == "month":
            # Changes nothing in storage, only which month the page is
            # reporting. Declared as an action rather than hidden in the page
            # so the month is a parameter of the same request that draws it.
            pass
        else:
            raise CommandError(f"Неизвестное действие: {action}")

        return self._view(identity, month)

    # -- what the page draws ------------------------------------------------

    def _view(self, identity: Identity, month: str) -> dict:
        """Assemble everything the page shows for one month.

        The shape below is the vocabulary: the web layer knows how to paint a
        chart, a form and a table, and knows nothing about spending. Adding a
        section here needs no change anywhere else.
        """
        readable = self._readable_as(identity)

        rows = self.repo.by_category(readable, month)
        total = sum((row["total"] for row in rows), Decimal("0"))
        previous_month = _shift_month(month, -1)
        previous = self.repo.monthly_total(readable, previous_month)

        categories = self.repo.categories(readable)

        return {
            "title": "Расходы",
            "month": {
                "value": month,
                "label": _month_label(month),
                "available": self.repo.available_months(readable),
            },
            "totals": {
                "current": _money(total),
                "previous": _money(previous),
                "change": self._change(total, previous),
                "has_previous": previous > 0,
            },
            "chart": self._chart(rows, total),
            "table": self._table(rows, total),
            "forms": self._forms(categories, identity),
            "recent": self._recent(readable),
        }

    def _change(self, total: Decimal, previous: Decimal) -> str:
        """How this month compares with the last one.

        Shown against spending rather than phrased as good or bad: whether a
        month is over or under depends on a budget this tool does not hold, and
        a report that editorialises it is reporting something it was not asked.
        """
        if previous <= 0:
            return ""

        delta = (total - previous) / previous * 100
        sign = "+" if delta >= 0 else "−"

        return f"{sign}{abs(delta):.1f}% к прошлому месяцу"

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

    def _table(self, rows: list[dict], total: Decimal) -> dict:
        """The same numbers in figures, under the chart.

        A bar shows the shape of a month; it cannot be read to the last digit.
        Both views come from one aggregation so they cannot disagree.
        """
        return {
            "columns": [
                {"key": "category", "label": "Категория"},
                {"key": "count", "label": "Трат"},
                {"key": "total", "label": "Сумма"},
                {"key": "share", "label": "Доля"},
            ],
            "rows": [
                {
                    "category": row["category"],
                    "count": row["count"],
                    "total": _money(row["total"]),
                    "share": f"{row['total'] / total * 100:.1f}%" if total > 0 else "—",
                }
                for row in rows
            ],
        }

    def _forms(self, categories: list[dict], identity: Identity) -> list[dict]:
        """The two forms the page offers, with the categories read live.

        The options come from the repository rather than from anything written
        down here, so a category created through any other path is in this list
        the moment it exists.
        """
        options = [
            {"value": str(category["id"]), "label": category["name"]}
            for category in categories
        ]

        return [
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
                        "value": date.today().isoformat(),
                    },
                    {"name": "note", "label": "Заметка", "type": "text"},
                ],
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

    def _recent(self, readable) -> list[dict]:
        entries = self.repo.recent(readable, limit=10)

        return [
            {
                "id": entry["id"],
                "amount": _money(entry["amount"]),
                "spent_at": entry["spent_at"].isoformat(),
                "category": entry["category"],
                "note": entry["note"] or "",
            }
            for entry in entries
        ]

    # -- actions ------------------------------------------------------------

    def _spend(self, identity: Identity, payload: dict) -> None:
        category_id = payload.get("category_id", "")
        amount = payload.get("amount", "")
        spent_at = payload.get("spent_at", "")
        note = payload.get("note", "")

        if not category_id:
            raise CommandError("Выберите категорию.")

        try:
            value = Decimal(amount.replace(" ", "").replace(",", "."))
        except (InvalidOperation, AttributeError):
            raise CommandError("Сумма должна быть числом.") from None

        if value <= 0:
            raise CommandError("Сумма должна быть больше нуля.")

        try:
            day = date.fromisoformat(spent_at) if spent_at else date.today()
        except ValueError:
            raise CommandError("Дата должна быть в формате ГГГГ-ММ-ДД.") from None

        try:
            self.repo.add(identity, int(category_id), value, day, note)
        except ValueError:
            # The repository refuses a category that is not this account's —
            # the id is a global sequence. Its message is about storage; the
            # person on the page only needs to know the category is not theirs.
            raise CommandError("Категория не найдена.") from None

    def _add_category(self, identity: Identity, payload: dict) -> None:
        name = payload.get("name", "").strip()

        if not name:
            raise CommandError("Название категории не может быть пустым.")

        if len(name) > MAX_CATEGORY_NAME:
            raise CommandError(
                f"Название длиннее {MAX_CATEGORY_NAME} символов.",
            )

        try:
            self.repo.add_category(self._readable_as(identity), name)
        except Exception as error:
            # The partial unique index is the rule, not the plugin: any other
            # writer has to obey it too. Its own message names a constraint in
            # a form nobody can act on, so the wording is ours.
            if "idx_expense_categories_active" in str(error):
                raise CommandError(f"Категория «{name}» уже есть.") from None
            raise

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

        self.repo.delete(identity, int(expense_id))

    # -- shared -------------------------------------------------------------

    def _readable_as(self, identity: Identity):
        """Whose spending this read covers.

        Widened across a linked pair, the same rule notes and clipboard follow,
        so an expense entered in the bot appears in the console. Falls back to
        the identity alone for a plugin built without a resolver.
        """
        if self._persons is None:
            return identity

        return self._persons.identities(identity)
