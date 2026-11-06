import os
import re
import unicodedata
from datetime import UTC
from zoneinfo import ZoneInfo

from app.core.config import Config
from app.core.identity import WEB, Identity
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.snippets_repo import KEEP, SnippetsRepository

from .base import BasePlugin

MAX_BYTES = 200_000
MAX_TITLE = 80
INLINE_LIMIT = 3500
LIST_SIZE = 10
TEXT_EXTENSIONS = {
    ".sql",
    ".txt",
    ".py",
    ".md",
    ".json",
    ".csv",
    ".yaml",
    ".yml",
    ".xml",
    ".js",
    ".ts",
    ".sh",
    ".log",
    ".ini",
    ".toml",
    ".html",
    ".css",
}
SOURCES = {"web": "сайт", "telegram": "Telegram"}


def _body(text) -> str:
    if not isinstance(text, str):
        raise CommandError("Текст должен быть строкой.")

    if "\x00" in text:
        raise CommandError("В тексте нулевой байт: похоже, это не текст.")

    if not text.strip():
        raise CommandError("Пустой текст.")

    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise CommandError("В тексте недопустимые символы.") from None

    if size > MAX_BYTES:
        raise CommandError(f"Текст больше {MAX_BYTES // 1000} КБ.")

    return text


def _title(raw, body: str) -> str:
    title = raw if isinstance(raw, str) else ""
    title = "".join(
        char for char in title if not unicodedata.category(char).startswith("C")
    ).strip()

    if not title:
        title = next((line.strip() for line in body.splitlines() if line.strip()), "")
        title = "".join(
            char for char in title if not unicodedata.category(char).startswith("C")
        )

    return title[:MAX_TITLE] or "Без названия"


def _decode(data: bytes) -> str:
    if b"\x00" in data:
        raise CommandError("Это не текстовый файл.")

    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CommandError("Файл не текстовый или не в UTF-8.") from None


def _filename(title: str) -> str:
    stem, ext = os.path.splitext(title)
    ext = ext.lower()

    if ext not in TEXT_EXTENSIONS:
        stem, ext = title, ".txt"

    stem = re.sub(r"[^\w.-]+", "_", stem).strip("._")[:60]

    return (stem or "snippet") + ext


def _lines(body: str) -> int:
    return body.count("\n") + 1


class SnippetsPlugin(BasePlugin):
    name = "snippets"
    version = "0.1.0"
    description = "Код и большие тексты между устройствами"
    page = "/snippets"
    page_title = "Сниппеты"

    commands = {
        "/snippets": "Последние сниппеты",
        "/snippet_get": "Прислать сниппет по номеру",
    }
    command_args = {"/snippet_get": [("number", "Номер")]}

    accepts_files = True
    max_file_bytes = MAX_BYTES

    def __init__(self, database: Database | None = None, persons=None):
        self.repo = SnippetsRepository(database)
        self._persons = persons

    async def execute(
        self, command: str, args: str, identity: Identity
    ) -> CommandResult:
        if command == "/snippets":
            return reply(self._listing(identity))

        if command == "/snippet_get":
            return self._send(identity, args)

        raise CommandError(f"Unknown command: {command}")

    async def receive_file(
        self, identity: Identity, filename: str, data: bytes
    ) -> CommandResult:
        if len(data) > MAX_BYTES:
            raise CommandError(f"Файл больше {MAX_BYTES // 1000} КБ.")

        body = _body(_decode(data))
        title = _title(os.path.basename(filename or ""), body)
        self.repo.add(identity, title, body, "telegram")

        return reply(f"Сохранено: {title}, строк: {_lines(body)}.")

    def _listing(self, identity: Identity) -> str:
        entries = self.repo.entries(self._readable_as(identity), LIST_SIZE)

        if not entries:
            return "Сниппетов нет. Пришлите файл или сохраните текст на сайте."

        lines = [
            f"{number}. {entry['title']} — строк: {entry['lines']}, "
            f"{_local(entry['created_at'])}"
            for number, entry in enumerate(entries, start=1)
        ]

        return "\n".join([*lines, "", "Прислать: /snippet_get <номер>"])

    def _send(self, identity: Identity, args: str) -> CommandResult:
        number = args.strip()

        if not number.isdigit() or not 1 <= int(number) <= KEEP:
            raise CommandError("Укажите номер из /snippets, например /snippet_get 1.")

        entries = self.repo.entries(self._readable_as(identity), int(number))

        if len(entries) < int(number):
            raise CommandError("Сниппета с таким номером нет.")

        snippet = self.repo.get(self._readable_as(identity), entries[-1]["id"])
        body = snippet["body"]

        if len(body) <= INLINE_LIMIT:
            return CommandResult(text=body, preformatted=True)

        return CommandResult(
            text=snippet["title"],
            file=(_filename(snippet["title"]), body.encode("utf-8")),
        )

    async def page_view(self, identity: Identity) -> dict:
        return self._view(identity, None)

    async def page_action(self, identity: Identity, action: str, payload: dict) -> dict:
        panel = None
        readable = self._readable_as(identity)

        if action == "save":
            body = _body(payload.get("body"))
            self.repo.add(
                identity, _title(payload.get("title"), body), body, _source(identity)
            )
        elif action == "open":
            snippet = self.repo.get(readable, _id(payload))
            if snippet is None:
                raise CommandError("Сниппет не найден.")
            panel = {
                "title": snippet["title"],
                "text": snippet["body"],
                "copy": payload.get("copy") == "1",
            }
        elif action == "delete":
            if not self.repo.delete(readable, _id(payload)):
                raise CommandError("Сниппет не найден.")
        elif action != "view":
            raise CommandError(f"Неизвестное действие: {action}")

        return self._view(identity, panel)

    def _view(self, identity: Identity, panel: dict | None) -> dict:
        view = {
            "title": "Сниппеты",
            "layout": ["panel", "forms", "list"],
            "forms": [
                {
                    "id": "save",
                    "title": "Новый сниппет",
                    "submit": "Сохранить",
                    "fields": [
                        {
                            "name": "title",
                            "label": "Название",
                            "type": "text",
                            "placeholder": "Необязательно",
                        },
                        {
                            "name": "body",
                            "label": "Текст или код",
                            "type": "textarea",
                            "required": True,
                        },
                    ],
                }
            ],
            "list": self._table(identity),
        }

        if panel:
            view["panel"] = panel

        return view

    def _table(self, identity: Identity) -> dict:
        rows = []

        for entry in self.repo.entries(self._readable_as(identity)):
            params = {"id": str(entry["id"])}
            rows.append(
                {
                    "title": entry["title"],
                    "lines": entry["lines"],
                    "size": f"{max(1, round(entry['bytes'] / 1000))} КБ",
                    "when": _local(entry["created_at"]),
                    "source": SOURCES.get(entry["source"], entry["source"]),
                    "actions": [
                        {
                            "action": "open",
                            "label": "Скопировать",
                            "kind": "ghost",
                            "params": {**params, "copy": "1"},
                        },
                        {
                            "action": "open",
                            "label": "Открыть",
                            "kind": "ghost",
                            "params": params,
                        },
                        {
                            "action": "delete",
                            "label": "Удалить",
                            "kind": "ghost",
                            "params": params,
                        },
                    ],
                }
            )

        return {
            "title": "Сохранённые",
            "empty": "Пока пусто. Вставьте текст выше или пришлите файл боту.",
            "columns": [
                {"key": "title", "label": "Название"},
                {"key": "lines", "label": "Строк", "num": True},
                {"key": "size", "label": "Размер", "num": True},
                {"key": "when", "label": "Когда"},
                {"key": "source", "label": "Откуда"},
            ],
            "rows": rows,
            "actions": "actions",
        }

    def _readable_as(self, identity: Identity):
        if self._persons is None:
            return identity

        return self._persons.identities(identity)


def _id(payload: dict) -> int:
    value = payload.get("id", "")

    if not isinstance(value, str) or not value.isdigit():
        raise CommandError("Сниппет не найден.")

    return int(value)


def _source(identity: Identity) -> str:
    return "web" if identity.kind == WEB else "telegram"


def _local(moment) -> str:
    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    return moment.replace(tzinfo=UTC).astimezone(zone).strftime("%d.%m %H:%M")
