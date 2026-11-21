import asyncio
import logging
import re
import secrets
import unicodedata
from collections.abc import AsyncIterator
from pathlib import Path

from app.core.config import Config
from app.core.identity import WEB, Identity
from app.core.results import CommandError, CommandResult
from app.db.database import Database
from app.db.files_repo import FilesRepository
from app.db.passkeys_repo import PasskeysRepository
from app.db.sessions_repo import digest
from app.db.users_repo import UsersRepository

from ..base import BasePlugin

logger = logging.getLogger(__name__)

MB = 2**20
MAX_FILE_BYTES = 50 * MB
TOTAL_BYTES = 500 * MB
MAX_NAME = 200
MAX_SHARED = 50
SWEEP_SECONDS = 600
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
STORED = re.compile(r"[0-9a-f]{32}(\.part)?")
MP4_BRANDS = {
    b"isom",
    b"iso2",
    b"iso4",
    b"iso5",
    b"iso6",
    b"mp41",
    b"mp42",
    b"avc1",
    b"M4V ",
}
SHARE_PREFIX = "/s/"


def sniff(head: bytes) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if head[4:8] == b"ftyp":
        if head[8:12] == b"qt  ":
            return "video/quicktime"
        if head[8:12] in MP4_BRANDS:
            return "video/mp4"
    return ""


def clean_name(raw: str) -> str:
    text = raw.replace("\\", "/").rsplit("/", 1)[-1]
    text = unicodedata.normalize("NFC", text)
    text = "".join(c for c in text if not unicodedata.category(c).startswith("C"))
    text = " ".join(text.split()).strip(". ")

    if not text:
        return "файл"

    if len(text) <= MAX_NAME:
        return text

    stem, dot, suffix = text.rpartition(".")
    if dot and 0 < len(suffix) <= 10:
        return stem[: MAX_NAME - len(suffix) - 1] + "." + suffix
    return text[:MAX_NAME]


def size_label(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < MB:
        return f"{size / 1024:.0f} КБ"
    return f"{size / MB:.1f} МБ".replace(".", ",")


def left_label(seconds: int) -> str:
    hours, rest = divmod(max(seconds, 0), 3600)
    if hours:
        return f"{hours} ч"
    return f"{max(rest // 60, 1)} мин"


def _ids(raw) -> list[int]:
    parts = raw.split(",") if isinstance(raw, str) else []
    if not parts or len(parts) > MAX_SHARED or not all(p.isdigit() for p in parts):
        raise CommandError("Отметьте файлы для ссылки.")
    return [int(p) for p in parts]


def _id(payload: dict) -> int:
    value = payload.get("id", "")
    if not isinstance(value, str) or not value.isdigit():
        raise CommandError("Файл не найден.")
    return int(value)


class FilesPlugin(BasePlugin):
    name = "files"
    version = "1.0.0"
    description = "Передача файлов между своими устройствами и по ссылке"
    page = "/files"
    page_title = "Файлы"
    page_script = str(Path(__file__).with_name("page.js"))
    public_prefix = SHARE_PREFIX
    web_only = True
    tables = {
        "files": "Файлы",
        "shares": "Ссылки на файлы",
        "share_files": "Файлы в ссылках",
    }

    def __init__(
        self,
        database: Database | None = None,
        persons=None,
        folder: Path | None = None,
    ):
        database = database or Database()
        self.repo = FilesRepository(database)
        self._users = UsersRepository(database)
        self._passkeys = PasskeysRepository(database)
        self._persons = persons
        self.folder = folder or Path(
            Config.FILES_DIR or Path(__file__).resolve().parents[3] / "data" / "files"
        )
        self._reserved = 0
        self._sweeper: asyncio.Task | None = None

    async def execute(
        self, command: str, args: str, identity: Identity
    ) -> CommandResult:
        raise CommandError("Файлы доступны только на сайте.")

    async def on_startup(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        known = self.repo.stored_names()
        for path in self.folder.iterdir():
            if STORED.fullmatch(path.name) and path.name not in known:
                path.unlink(missing_ok=True)
        self._sweeper = asyncio.create_task(self._sweep_forever())

    async def on_shutdown(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()

    async def _sweep_forever(self) -> None:
        while True:
            try:
                self.sweep()
            except Exception:
                logger.exception("Files sweep failed")
            await asyncio.sleep(SWEEP_SECONDS)

    def sweep(self) -> None:
        for stored in self.repo.purge():
            self.path(stored).unlink(missing_ok=True)

    def path(self, stored: str) -> Path:
        return self.folder / stored

    def may_share(self, identity: Identity) -> bool:
        if identity.kind != WEB:
            return False
        user = self._users.get_by_username(identity.id)
        return user is not None and user.is_admin and self._passkeys.has_any(user.id)

    async def store(
        self,
        identity: Identity,
        raw_name: str,
        size: int,
        chunks: AsyncIterator[bytes],
    ) -> dict:
        if size <= 0:
            raise CommandError("Файл пустой.")
        if size > MAX_FILE_BYTES:
            raise CommandError(f"Файл больше {MAX_FILE_BYTES // MB} МБ.")
        if self.repo.used_bytes() + self._reserved + size > TOTAL_BYTES:
            raise CommandError(
                "Место для файлов закончилось. Удалите ненужные "
                "или подождите: файлы удаляются через сутки."
            )

        self._reserved += size
        stored = secrets.token_hex(16)
        part = self.folder / f"{stored}.part"
        received = 0
        head = b""

        try:
            with part.open("wb") as out:
                async for chunk in chunks:
                    received += len(chunk)
                    if received > size:
                        raise CommandError("Файл пришёл не целиком.")
                    if len(head) < 16:
                        head = (head + chunk)[:16]
                    out.write(chunk)
            if received != size:
                raise CommandError("Файл пришёл не целиком.")
            part.rename(self.path(stored))
            entry = self.repo.add(
                identity, clean_name(raw_name), size, sniff(head), stored
            )
        except BaseException:
            part.unlink(missing_ok=True)
            self.path(stored).unlink(missing_ok=True)
            raise
        finally:
            self._reserved -= size

        self.changed(identity)
        return entry

    def own(self, identity: Identity, file_id: int) -> dict | None:
        return self.repo.get(self._readable_as(identity), file_id)

    def shared(self, token: str, file_id: int | None = None) -> list[dict]:
        if not TOKEN.fullmatch(token):
            return []
        return self.repo.shared(digest(token), file_id)

    async def page_view(self, identity: Identity) -> dict:
        return self._view(self._readable_as(identity), self.may_share(identity), None)

    async def page_action(self, identity: Identity, action: str, payload: dict) -> dict:
        readable = self._readable_as(identity)
        sharer = self.may_share(identity)
        link = None

        if action == "delete":
            stored = self.repo.delete(readable, _id(payload))
            if stored is None:
                raise CommandError("Файл не найден.")
            self.path(stored).unlink(missing_ok=True)
        elif action == "share":
            link = self._share(identity, readable, sharer, payload.get("ids"))
        elif action == "revoke":
            if not sharer or not self.repo.revoke(readable, _id(payload)):
                raise CommandError("Ссылка не найдена.")
        elif action != "view":
            raise CommandError(f"Неизвестное действие: {action}")

        return self._view(readable, sharer, link)

    def _share(self, identity: Identity, readable, sharer: bool, raw_ids) -> str:
        if not sharer:
            raise CommandError("Публичные ссылки создаёт только администратор.")

        token = secrets.token_urlsafe(32)

        if not self.repo.share(identity, readable, _ids(raw_ids), digest(token)):
            raise CommandError("Файлы не найдены: возможно, их уже удалили.")

        return f"{Config.PUBLIC_BASE_URL}{SHARE_PREFIX}{token}"

    def _view(self, readable, sharer: bool, link: str | None) -> dict:
        used = self.repo.used_bytes()
        files = {
            "title": f"Занято {size_label(used)} из {TOTAL_BYTES // MB} МБ",
            "empty": "Файлов нет. Каждый файл удаляется через сутки.",
            "compact": True,
            "columns": [
                *([{"key": "id", "label": "Выбрать", "pick": True}] if sharer else []),
                {"key": "name", "label": "Файл"},
                {"key": "size", "label": "Размер", "nowrap": True},
                {"key": "left", "label": "Удалится через", "nowrap": True},
                {"key": "url", "label": "", "link": True, "text": "Скачать"},
            ],
            "actions": "actions",
            "rows": [
                {
                    "id": str(entry["id"]),
                    "name": entry["name"],
                    "size": size_label(entry["size"]),
                    "left": left_label(entry["left"]),
                    "url": f"/files/get/{entry['id']}",
                    "actions": [
                        {
                            "action": "delete",
                            "label": "Удалить",
                            "kind": "ghost",
                            "params": {"id": str(entry["id"])},
                        }
                    ],
                }
                for entry in self.repo.entries(readable)
            ],
        }

        tables = [files]

        if link:
            tables.append(
                {
                    "title": "Новая ссылка — показана один раз",
                    "columns": [
                        {"key": "url", "label": "Ссылка", "link": True, "copy": True},
                        {"key": "note", "label": ""},
                    ],
                    "rows": [
                        {
                            "url": link,
                            "note": "Скопируйте её сейчас: потом её не посмотреть.",
                        }
                    ],
                }
            )

        if sharer:
            files["tools"] = [
                {"action": "share", "label": "Ссылка на отмеченные", "picked": "ids"}
            ]
            tables.append(self._shares_table(readable))

        view = {
            "title": "Файлы",
            "live": True,
            "layout": ["list"],
            "list": tables,
        }

        if link:
            view["hold"] = True

        return view

    def _shares_table(self, readable) -> dict:
        return {
            "title": "Публичные ссылки",
            "columns": [
                {"key": "files", "label": "Файлов"},
                {"key": "left", "label": "Удалится через", "nowrap": True},
            ],
            "actions": "actions",
            "rows": [
                {
                    "files": str(share["files"]),
                    "left": left_label(share["left"]),
                    "actions": [
                        {
                            "action": "revoke",
                            "label": "Отключить",
                            "kind": "ghost",
                            "params": {"id": str(share["id"])},
                        }
                    ],
                }
                for share in self.repo.shares(readable)
            ],
        }

    def web_routes(self, identify) -> list:
        from .web import FileRoutes

        return FileRoutes(self, identify).routes()
