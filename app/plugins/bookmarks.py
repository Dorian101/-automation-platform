import asyncio
import html
import ipaddress
import re
import socket
import unicodedata
from datetime import UTC
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver
from yarl import URL

from app.core.config import Config
from app.core.identity import WEB, Identity, is_guest
from app.core.results import CommandError, CommandResult, reply
from app.db.api_keys_repo import MAX_KEYS, ApiKeysRepository, TooManyKeys
from app.db.bookmarks_repo import BookmarksRepository
from app.db.database import Database

from .base import BasePlugin

MAX_URL = 2000
MAX_TITLE = 200
MAX_DESCRIPTION = 1000
LIST_SIZE = 10
FETCH_BYTES = 256_000
FETCH_SECONDS = 8
REDIRECTS = 4
GUEST_BOOKMARK_LIMIT = 30
DEMO_BOOKMARKS = [
    (
        "https://developer.mozilla.org/ru/docs/Web/HTTP/CSP",
        "Content Security Policy (CSP) - HTTP | MDN",
        "nonce вместо unsafe-inline",
        False,
    ),
    (
        "https://docs.python.org/3/library/ipaddress.html",
        "ipaddress — IPv4/IPv6 manipulation library",
        "is_global и IPv4-mapped адреса",
        False,
    ),
    (
        "https://www.postgresql.org/docs/current/sql-insert.html",
        "PostgreSQL: Documentation: INSERT",
        "ON CONFLICT DO UPDATE",
        True,
    ),
    (
        "https://core.telegram.org/bots/api",
        "Telegram Bot API",
        "MessageEntity: url и text_link",
        False,
    ),
    (
        "https://github.com/astral-sh/uv",
        "GitHub - astral-sh/uv: An extremely fast Python package and project manager",
        "",
        True,
    ),
]
FETCH_PARALLEL = 3
TRACKING = {
    "fbclid",
    "gclid",
    "yclid",
    "ysclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "_openstat",
}


def normalize(raw: str) -> str:
    text = raw.strip() if isinstance(raw, str) else ""

    if not text or len(text) > MAX_URL or any(char.isspace() for char in text):
        raise CommandError("Это не похоже на ссылку.")

    if "://" not in text:
        text = "https://" + text

    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise CommandError("Это не похоже на ссылку.") from None

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")

    if (
        scheme not in ("http", "https")
        or not host
        or "." not in host
        and ":" not in host
    ):
        raise CommandError("Сохраняются только ссылки http и https.")

    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        host = host.lower()

    if ":" in host:
        host = f"[{host}]"

    if port and port != {"http": 80, "https": 443}[scheme]:
        host = f"{host}:{port}"

    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair and not _tracking(pair.split("=", 1)[0].lower())
    )
    path = parts.path.rstrip("/")

    return urlunsplit((scheme, host, path, query, parts.fragment))


def _tracking(key: str) -> bool:
    return key.startswith("utm_") or key in TRACKING


def domain(url: str) -> str:
    host = urlsplit(url).hostname or url

    try:
        host = host.encode("ascii").decode("idna")
    except UnicodeError:
        pass

    return host.removeprefix("www.")


def _clean(raw, limit: int) -> str:
    text = raw if isinstance(raw, str) else ""
    text = "".join(
        " " if char in "\n\t\r" else char
        for char in text
        if char in "\n\t\r" or not unicodedata.category(char).startswith("C")
    )

    return " ".join(text.split())[:limit]


class _PublicResolver(AbstractResolver):
    def __init__(self):
        self._inner = ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        found = [
            one
            for one in await self._inner.resolve(host, port, family)
            if _public(one["host"])
        ]

        if not found:
            raise OSError(f"{host} is not a public address")

        return found

    async def close(self):
        await self._inner.close()


TRANSLATED = [
    ipaddress.ip_network(net) for net in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96")
]


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])

    if ip.version == 6:
        if ip.ipv4_mapped or any(ip in net for net in TRANSLATED):
            return False

    return ip.is_global


def _public_literal(host: str) -> bool:
    try:
        return _public(host.strip("[]"))
    except ValueError:
        return True


TITLE = re.compile(r"<title[^>]*>(.*?)</title", re.I | re.S)
OG_TITLE = re.compile(
    r"<meta\s[^>]*?(?:property|name)\s*=\s*[\"']og:title[\"'][^>]*>", re.I
)
CONTENT = re.compile(r"content\s*=\s*(\"[^\"]*\"|'[^']*')", re.I)
META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)", re.I)


def extract_title(body: bytes, charset: str | None) -> str:
    if not charset:
        found = META_CHARSET.search(body[:4096])
        charset = found.group(1).decode("ascii") if found else "utf-8"

    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")

    og = OG_TITLE.search(text)
    content = CONTENT.search(og.group(0)) if og else None

    if content:
        title = content.group(1)[1:-1]
    else:
        found = TITLE.search(text)
        title = found.group(1) if found else ""

    return _clean(html.unescape(title), MAX_TITLE)


async def fetch_title(url: str) -> str:
    connector = aiohttp.TCPConnector(resolver=_PublicResolver(), limit=2)
    timeout = aiohttp.ClientTimeout(total=FETCH_SECONDS)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AutomationPlatformBookmarks/1.0)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ru,en;q=0.8",
    }

    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers=headers
    ) as session:
        for _ in range(REDIRECTS + 1):
            try:
                target = URL(url)
                port = target.port
            except (ValueError, TypeError):
                return ""

            if (
                target.scheme not in ("http", "https")
                or port not in (80, 443)
                or not target.host
                or not _public_literal(target.host)
            ):
                return ""

            async with session.get(target, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        return ""
                    url = urljoin(str(target), location)
                    continue

                if response.status != 200 or "html" not in response.content_type:
                    return ""

                body, size = [], 0
                while size < FETCH_BYTES:
                    chunk = await response.content.read(FETCH_BYTES - size)
                    if not chunk:
                        break
                    body.append(chunk)
                    size += len(chunk)

                return extract_title(b"".join(body), response.charset)

    return ""


KEY_FORM = {
    "id": "keycreate",
    "title": "Ключ для iPhone",
    "submit": "Создать ключ",
    "fields": [
        {"name": "name", "label": "Название", "type": "text", "value": "iPhone"}
    ],
}


class BookmarksPlugin(BasePlugin):
    name = "bookmarks"
    tables = {"bookmarks": "Ссылки"}
    version = "0.1.0"
    description = "Ссылки на потом: из бота, с iPhone и кнопкой в браузере"
    page = "/bookmarks"
    page_title = "Ссылки"
    demo = True
    check = "/links"

    commands = {"/links": "Последние ссылки"}

    accepts_links = True

    def __init__(
        self, database: Database | None = None, persons=None, fetch=fetch_title
    ):
        self.repo = BookmarksRepository(database)
        self.keys = ApiKeysRepository(database)
        self._persons = persons
        self._fetch = fetch
        self._pending: set[asyncio.Task] = set()
        self._fetching = asyncio.Semaphore(FETCH_PARALLEL)

    async def execute(
        self, command: str, args: str, identity: Identity
    ) -> CommandResult:
        if command == "/links":
            return reply(self._listing(identity))

        raise CommandError(f"Unknown command: {command}")

    async def receive_links(
        self, identity: Identity, urls: list[str], text: str
    ) -> CommandResult:
        description = text
        for url in urls:
            description = description.replace(url, " ")
        description = _clean(description, MAX_DESCRIPTION)

        readable = self._readable_as(identity)
        lines = []
        for url in dict.fromkeys(urls):
            try:
                entry, existed = self._save(identity, readable, url, description)
            except CommandError as error:
                lines.append(f"{url[:80]}: {error}")
                continue

            name = entry["title"] or domain(entry["url"])
            lines.append(
                f"Уже было, поднял наверх: {name}" if existed else f"Сохранено: {name}"
            )

        return reply("\n".join(lines))

    async def seed_demo(self, identity: Identity) -> None:
        for url, title, description, read in DEMO_BOOKMARKS:
            entry, _ = self.repo.save(identity, identity, url, description, "web")
            self.repo.fill_title(entry["id"], title)
            if read:
                self.repo.mark(identity, entry["id"], True)

    async def on_shutdown(self) -> None:
        for task in self._pending:
            task.cancel()

        await asyncio.gather(*self._pending, return_exceptions=True)

    def _save(self, identity: Identity, readable, raw, description: str):
        url = normalize(raw)
        guest = is_guest(identity)

        if guest and len(self.repo.entries(readable)) >= GUEST_BOOKMARK_LIMIT:
            raise CommandError(
                f"Демо ограничено {GUEST_BOOKMARK_LIMIT} ссылками. "
                "Удалите лишние, чтобы добавить новые."
            )

        entry, existed = self.repo.save(
            identity, readable, url, description, _source(identity)
        )

        if not entry["title"] and not guest:
            task = asyncio.create_task(self._title(identity, entry["id"], url))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

        return entry, existed

    async def _title(self, identity: Identity, bookmark_id: int, url: str) -> None:
        try:
            async with self._fetching:
                title = _clean(await self._fetch(url), MAX_TITLE)
        except Exception:
            return

        if title:
            await asyncio.to_thread(self.repo.fill_title, bookmark_id, title)
            self.changed(identity)

    def _listing(self, identity: Identity) -> str:
        entries = self.repo.entries(self._readable_as(identity), LIST_SIZE)

        if not entries:
            return "Ссылок нет. Пришлите боту любую ссылку, и она сохранится."

        return "\n\n".join(
            f"{number}. {'✓ ' if entry['is_read'] else ''}"
            f"{entry['title'] or domain(entry['url'])}\n{entry['url']}"
            for number, entry in enumerate(entries, start=1)
        )

    async def page_view(self, identity: Identity) -> dict:
        return self._view(identity, self._readable_as(identity), None)

    async def page_action(self, identity: Identity, action: str, payload: dict) -> dict:
        readable = self._readable_as(identity)
        editing = None
        notice = None

        if action == "save":
            _, existed = self._save(
                identity,
                readable,
                payload.get("url"),
                _clean(payload.get("description"), MAX_DESCRIPTION),
            )
            if existed:
                notice = "Эта ссылка уже была, поднял её наверх."
        elif action in ("read", "unread"):
            if not self.repo.mark(readable, _id(payload), action == "read"):
                raise CommandError("Ссылка не найдена.")
        elif action == "edit":
            editing = self.repo.get(readable, _id(payload))
            if editing is None:
                raise CommandError("Ссылка не найдена.")
        elif action == "update":
            if not self.repo.update(
                readable,
                _id(payload),
                _clean(payload.get("title"), MAX_TITLE),
                _clean(payload.get("description"), MAX_DESCRIPTION),
            ):
                raise CommandError("Ссылка не найдена.")
            notice = "Изменения сохранены."
        elif action == "delete":
            if not self.repo.delete(readable, _id(payload)):
                raise CommandError("Ссылка не найдена.")
        elif action.startswith("key") and is_guest(identity):
            raise CommandError("В демо ключей нет: они привязаны к учётной записи.")
        elif action == "keycreate":
            notice = self._create_key(identity, payload.get("name"))
        elif action == "keyrevoke":
            if not self.keys.revoke(identity, _id(payload)):
                raise CommandError("Ключ не найден.")
            notice = "Ключ отозван."
        elif action != "view":
            raise CommandError(f"Неизвестное действие: {action}")

        view = self._view(identity, readable, editing)

        if notice:
            view["notice"] = notice

        if action == "keycreate":
            view["hold"] = True

        return view

    def _create_key(self, identity: Identity, raw_name) -> str:
        name = _clean(raw_name, 40) or "Ключ"

        try:
            key = self.keys.create(identity, name)
        except TooManyKeys:
            raise CommandError(
                f"Ключей не больше {MAX_KEYS}. Отзовите ненужный."
            ) from None

        if key is None:
            raise CommandError("Ключ можно создать только на сайте.")

        return (
            f"Ключ «{name}»: {key} — он показан один раз. "
            "Скопируйте его в команду «В ссылки» на iPhone."
        )

    def _view(self, identity: Identity, readable, editing: dict | None) -> dict:
        if editing:
            form = {
                "id": "update",
                "title": "Правка ссылки",
                "submit": "Сохранить",
                "cancel": {"action": "view", "label": "Отмена"},
                "fields": [
                    {"name": "id", "type": "hidden", "value": str(editing["id"])},
                    {
                        "name": "title",
                        "label": "Заголовок",
                        "type": "text",
                        "value": editing["title"],
                        "placeholder": domain(editing["url"]),
                    },
                    {
                        "name": "description",
                        "label": "Описание",
                        "type": "text",
                        "value": editing["description"],
                    },
                ],
            }
        else:
            form = {
                "id": "save",
                "title": "Новая ссылка",
                "submit": "Сохранить",
                "fields": [
                    {
                        "name": "url",
                        "label": "Ссылка",
                        "type": "url",
                        "required": True,
                        "placeholder": "https://…",
                    },
                    {
                        "name": "description",
                        "label": "Описание",
                        "type": "text",
                        "placeholder": "Необязательно",
                    },
                ],
            }

        return {
            "title": "Ссылки",
            "live": not is_guest(identity),
            "layout": ["forms", "list"],
            "forms": [form] if is_guest(identity) else [form, KEY_FORM],
            "list": [self._table(readable)]
            if is_guest(identity)
            else [self._table(readable), self._keys_table(identity)],
        }

    def _keys_table(self, identity: Identity) -> dict:
        return {
            "title": "Ключи для iPhone",
            "empty": "Ключей нет. Кнопка для браузера — на странице /links/save.",
            "columns": [
                {"key": "name", "label": "Название"},
                {"key": "created", "label": "Создан"},
                {"key": "used", "label": "Последний раз"},
            ],
            "rows": [
                {
                    "name": key["name"],
                    "created": _local(key["created_at"]),
                    "used": _local(key["last_used_at"]) if key["last_used_at"] else "",
                    "actions": [
                        {
                            "action": "keyrevoke",
                            "label": "Отозвать",
                            "kind": "ghost",
                            "params": {"id": str(key["id"])},
                        }
                    ],
                }
                for key in self.keys.entries(identity)
            ],
            "actions": "actions",
        }

    def _table(self, readable) -> dict:
        rows = []

        for entry in self.repo.entries(readable):
            params = {"id": str(entry["id"])}
            site = domain(entry["url"])
            toggle = (
                ("unread", "Не прочитано")
                if entry["is_read"]
                else ("read", "Прочитано")
            )
            rows.append(
                {
                    "title": entry["title"] or site,
                    "site": site,
                    "description": entry["description"],
                    "url": entry["url"],
                    "state": "прочитано" if entry["is_read"] else "",
                    "when": _local(entry["saved_at"]),
                    "actions": [
                        {
                            "action": action,
                            "label": label,
                            "kind": "ghost",
                            "params": params,
                        }
                        for action, label in (
                            toggle,
                            ("edit", "Изменить"),
                            ("delete", "Удалить"),
                        )
                    ],
                }
            )

        return {
            "title": "Сохранённые",
            "empty": "Пока пусто. Добавьте ссылку выше или пришлите её боту.",
            "columns": [
                {"key": "title", "label": "Заголовок"},
                {"key": "site", "label": "Сайт"},
                {"key": "description", "label": "Описание"},
                {"key": "url", "label": "Ссылка", "link": True},
                {"key": "state", "label": "Статус"},
                {"key": "when", "label": "Когда"},
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
        raise CommandError("Ссылка не найдена.")

    return int(value)


def _source(identity: Identity) -> str:
    return "web" if identity.kind == WEB else "telegram"


def _local(moment) -> str:
    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    return moment.replace(tzinfo=UTC).astimezone(zone).strftime("%d.%m %H:%M")
