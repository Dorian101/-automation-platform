"""Job applications, shown as a funnel over the pipeline.

Web-only with a page, for the same reason spending has one: this is a report,
and a report typed into a chat line by chat line is a worse report. Declared
rather than checked inside the plugin — see DECISIONS 2026-10-10.

The statuses form a state machine, and that is the part worth being explicit
about. A status is not a free label a person picks from a dropdown: each
stage knows which stages may follow it, and a move that is not offered is a
move that is refused. It keeps the funnel honest — an application that
cannot go backwards cannot quietly re-enter the first stage and make the
conversion look better than it is — and it is the thing to say out loud on an
interview about a statuses column.

The funnel itself is one group-by. Everything drawn on the page, shares and
conversion percentages included, is derived from those numbers here rather
than counted in the browser, so the chart and the table under it are two
views of one aggregation and cannot disagree.

Silence is measured only while an answer is outstanding. Days counted from an
application that already got a reply would be a number about the past, shown
next to a decision that is about now.
"""

from datetime import date

from app.core.identity import Identity
from app.core.results import CommandError, CommandResult, reply
from app.db.applications_repo import ApplicationsRepository
from app.db.database import Database

from .base import BasePlugin

# A stage, the label it is shown under, and the stages it may move to.
#
# `rejected` and `offer` are terminal: there is nowhere to go from an offer
# except keeping it. A rejected application stays in the list rather than
# disappearing, because the count of rejections is part of what the funnel
# reports, and a record that vanishes on rejection is a record that cannot be
# reasoned about later.
STAGES = {
    "applied": ("Отклик отправлен", ("answered", "rejected")),
    "answered": ("Ответили", ("interview", "rejected")),
    "interview": ("Собеседование", ("offer", "rejected")),
    "offer": ("Оффер", ()),
    "rejected": ("Отказ", ()),
}

# Days without an answer before a row is worth noticing. Not a warning and
# not a judgement — the job market takes as long as it takes — just a way of
# separating "waiting" from "forgotten".
SILENCE_DAYS = 5

MAX_COMPANY = 80
MAX_ROLE = 80


def _label(status: str) -> str:
    return STAGES.get(status, (status, ()))[0]


def _next_stages(status: str) -> tuple[str, ...]:
    return STAGES.get(status, (status, ()))[1]


class ApplicationsPlugin(BasePlugin):
    name = "applications"
    version = "0.1.0"
    description = "Track job applications"
    web_only = True
    page = "/applications"
    page_title = "Отклики"

    commands = {
        "/apps": "Show the application list (web console)",
        "/appadd": "Record an application (web console)",
    }

    def __init__(self, database: Database | None = None, persons=None):
        self.repo = ApplicationsRepository(database)
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
        Present because the contract requires the method.
        """
        return reply("Applications are on the web console.")

    # -- the page -----------------------------------------------------------

    async def page_view(self, identity: Identity) -> dict:
        return self._view(identity)

    async def page_action(
        self,
        identity: Identity,
        action: str,
        payload: dict,
    ) -> dict:
        if action == "add":
            self._add(identity, payload)
        elif action == "stage":
            self._set_stage(identity, payload)
        elif action == "delete":
            self._delete(identity, payload)
        else:
            raise CommandError(f"Неизвестное действие: {action}")

        return self._view(identity)

    # -- what the page draws ------------------------------------------------

    def _view(self, identity: Identity) -> dict:
        readable = self._readable_as(identity)
        entries = self.repo.entries(readable)
        counts = self.repo.counts_by_status(readable)

        return {
            "title": "Отклики",
            "totals": self._totals(entries),
            "funnel": self._funnel(counts),
            "table": self._table(entries),
            "forms": self._forms(),
        }

    def _totals(self, entries: list[dict]) -> dict:
        """How many are outstanding, and how many have gone quiet."""
        waiting = sum(1 for e in entries if e["status"] == "applied")
        silent = sum(1 for e in entries if self._silent_days(e) is not None)

        return {
            "total": len(entries),
            "waiting": waiting,
            "silent": silent,
            "silent_note": (
                f"молчат дольше {SILENCE_DAYS} дн." if silent else ""
            ),
        }

    def _funnel(self, counts: dict[str, int]) -> dict:
        """The pipeline, as the share of applications that reached each stage.

        A status is the stage an application is in *now*, so an application
        sitting at "interview" has already passed "answered". Counting current
        stages directly would therefore show 2 answered out of 2 still
        waiting — a hundred percent, from four applications where half
        progressed. Each stage is the sum of that stage and every stage after
        it, which is what makes the numbers read as a funnel.

        The denominator is everything ever sent, rejections included: they
        left the pipeline, and excluding them would quietly flatter every
        later stage. Rejections are reported next to the funnel rather than
        drawn as one — they are an exit, not a rung, and a bar there would
        mean the widths no longer lined up.
        """
        total = sum(counts.values())
        answered = (
            counts.get("answered", 0)
            + counts.get("interview", 0)
            + counts.get("offer", 0)
        )
        interview = counts.get("interview", 0) + counts.get("offer", 0)
        offer = counts.get("offer", 0)

        stages = (
            ("applied", "Откликов отправлено", total),
            ("answered", "Ответили", answered),
            ("interview", "Дошли до собеседования", interview),
            ("offer", "Получили оффер", offer),
        )

        items = []
        for status, label, count in stages:
            items.append(
                {
                    "status": status,
                    "label": label,
                    "count": count,
                    "share": (count / total * 100) if total else 0.0,
                    "share_text": f"{count / total * 100:.0f}%" if total else "—",
                }
            )

        return {
            "kind": "funnel",
            "items": items,
            "rejected": {
                "label": _label("rejected"),
                "count": counts.get("rejected", 0),
            },
            "empty": "Откликов пока нет",
        }

    def _table(self, entries: list[dict]) -> dict:
        """The same applications as rows, with the moves each one allows.

        The buttons are what makes the state machine visible: an application
        in a terminal stage simply has none, so there is no control offering a
        move that would be refused.
        """
        today = date.today()

        rows = []
        for entry in entries:
            silence = self._silent_days(entry)
            rows.append(
                {
                    "id": entry["id"],
                    "company": entry["company"],
                    "role": entry["role"],
                    "url": entry["url"] or "",
                    "applied_at": entry["applied_at"].isoformat(),
                    "status": entry["status"],
                    "status_label": _label(entry["status"]),
                    "note": entry["note"] or "",
                    "age_days": (today - entry["applied_at"]).days,
                    # None rather than 0: a row that is not waiting has no
                    # silence to report, and zero would read as "answered
                    # today".
                    "silent_days": silence,
                    "silent": silence is not None,
                    "moves": [
                        {"status": target, "label": _label(target)}
                        for target in _next_stages(entry["status"])
                    ],
                }
            )

        return {
            "columns": [
                {"key": "company", "label": "Компания"},
                {"key": "role", "label": "Роль"},
                {"key": "applied_at", "label": "Дата"},
                {"key": "age_days", "label": "Дней"},
                {"key": "status_label", "label": "Статус"},
                {"key": "silent_days", "label": "Молчание"},
            ],
            "rows": rows,
        }

    def _silent_days(self, entry: dict) -> int | None:
        """Days since an application was sent, while none has arrived.

        None for every stage that already involves a reply. Counting on past
        a rejection would keep the number growing forever on a row that can
        never change again, and it would say nothing about what is waiting.
        """
        if entry["status"] != "applied":
            return None

        days = (date.today() - entry["applied_at"]).days

        return days if days >= SILENCE_DAYS else None

    def _forms(self) -> list[dict]:
        return [
            {
                "id": "add",
                "title": "Добавить отклик",
                "submit": "Добавить",
                "fields": [
                    {
                        "name": "company",
                        "label": "Компания",
                        "type": "text",
                        "placeholder": "ООО Ромашка",
                        "required": True,
                    },
                    {
                        "name": "role",
                        "label": "Роль",
                        "type": "text",
                        "placeholder": "BI-аналитик",
                        "required": True,
                    },
                    {
                        "name": "url",
                        "label": "Ссылка",
                        "type": "text",
                        "placeholder": "https://hh.ru/vacancy/123",
                    },
                    {
                        "name": "applied_at",
                        "label": "Дата отклика",
                        "type": "date",
                        "value": date.today().isoformat(),
                        "sticky": True,
                    },
                    {"name": "note", "label": "Заметка", "type": "text"},
                ],
            }
        ]

    # -- actions ------------------------------------------------------------

    def _add(self, identity: Identity, payload: dict) -> None:
        company = payload.get("company", "").strip()
        role = payload.get("role", "").strip()
        url = payload.get("url", "").strip()
        note = payload.get("note", "").strip()
        applied_at = payload.get("applied_at", "")

        if not company:
            raise CommandError("Укажите компанию.")

        if not role:
            raise CommandError("Укажите роль.")

        if len(company) > MAX_COMPANY or len(role) > MAX_ROLE:
            raise CommandError("Слишком длинное название.")

        if applied_at:
            try:
                day = date.fromisoformat(applied_at)
            except ValueError:
                raise CommandError(
                    "Дата должна быть в формате ГГГГ-ММ-ДД.",
                ) from None
        else:
            day = date.today()

        if day > date.today():
            # An application dated in the future is a typo, and it would sit
            # in the list counting days that have not happened.
            raise CommandError("Дата отклика не может быть в будущем.")

        self.repo.add(identity, company, role, day, url, note)

    def _set_stage(self, identity: Identity, payload: dict) -> None:
        application_id = payload.get("id", "")
        target = payload.get("status", "")

        if not application_id.isdigit():
            raise CommandError("Отклик не найден.")

        if target not in STAGES:
            raise CommandError("Неизвестный статус.")

        entry = self._find(identity, int(application_id))

        if entry is None:
            raise CommandError("Отклик не найден.")

        allowed = _next_stages(entry["status"])

        if target not in allowed:
            # Named as a refusal rather than silently ignored: the button is
            # not on the page, so reaching here means a stale page or a
            # hand-made request, and both are worth saying no to out loud.
            raise CommandError(
                f"Нельзя перевести из «{_label(entry['status'])}» "
                f"в «{_label(target)}».",
            )

        self.repo.set_status(identity, int(application_id), target)

    def _delete(self, identity: Identity, payload: dict) -> None:
        application_id = payload.get("id", "")

        if not application_id.isdigit():
            raise CommandError("Отклик не найден.")

        if not self.repo.delete(identity, int(application_id)):
            raise CommandError("Отклик не найден.")

    def _find(self, identity: Identity, application_id: int) -> dict | None:
        for entry in self.repo.entries(self._readable_as(identity)):
            if entry["id"] == application_id:
                return entry

        return None

    # -- shared -------------------------------------------------------------

    def _readable_as(self, identity: Identity):
        """Whose applications this read covers.

        Widened across a linked pair like notes, clipboard and expenses. Falls
        back to the identity alone for a plugin built without a resolver.
        """
        if self._persons is None:
            return identity

        return self._persons.identities(identity)
