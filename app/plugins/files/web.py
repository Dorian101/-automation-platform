import html
from pathlib import Path
from urllib.parse import quote, unquote

from aiohttp import web

from app.core.results import CommandError
from app.web.common import rate_key
from app.web.pages import page_styles, render_not_found
from app.web.ratelimit import RateLimiter

from . import SHARE_PREFIX, size_label

CHUNK = 2**16
UPLOADS_PER_HOUR = 200
MISSES_PER_HOUR = 20
HOUR = 3600
FILE_POLICY = "default-src 'none'; sandbox; frame-ancestors 'none'"
PAGE_POLICY = (
    "default-src 'none'; img-src 'self'; media-src 'self'; font-src 'self'; "
    "style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
PRIVATE = {
    "Cache-Control": "private, no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Cross-Origin-Resource-Policy": "same-origin",
}
TEMPLATE = Path(__file__).with_name("share.html")


def disposition(kind: str, name: str) -> str:
    fallback = (
        "".join(
            c
            for c in name.encode("ascii", "ignore").decode()
            if c not in '"\\' and c.isprintable()
        )
        or "file"
    )
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


class FileRoutes:
    def __init__(self, plugin, identify):
        self._plugin = plugin
        self._identify = identify
        self._rate = RateLimiter()

    def routes(self) -> list:
        return [
            web.post("/files/upload", self._upload),
            web.get("/files/get/{id:[0-9]+}", self._own_file),
            web.get(SHARE_PREFIX + "{token}", self._share_page),
            web.get(SHARE_PREFIX + "{token}/{id:[0-9]+}", self._shared_file),
        ]

    async def _upload(self, request: web.Request) -> web.Response:
        identity = self._identify(request)

        if identity is None:
            return web.json_response({"error": "Unauthorized"}, status=401)

        name = request.headers.get("X-File-Name")
        size = request.content_length

        if name is None or size is None:
            return web.json_response(
                {"error": "Нет имени или размера файла."}, status=400
            )

        if not self._rate.allow(
            rate_key(request, "files-upload", str(identity)), UPLOADS_PER_HOUR, HOUR
        ):
            return web.json_response(
                {"error": "Слишком много загрузок, подождите."}, status=429
            )

        try:
            entry = await self._plugin.store(
                identity, unquote(name), size, request.content.iter_chunked(CHUNK)
            )
        except CommandError as error:
            return web.json_response({"error": str(error)}, status=400)

        return web.json_response({"id": entry["id"]})

    async def _own_file(self, request: web.Request) -> web.StreamResponse:
        identity = self._identify(request)
        entry = identity and self._plugin.own(identity, int(request.match_info["id"]))

        if not entry:
            return web.Response(
                text=render_not_found(), content_type="text/html", status=404
            )

        return self._send(entry, inline=False)

    async def _share_page(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        entries = self._plugin.shared(token)

        if not entries:
            return self._miss(request)

        items = []
        for entry in entries:
            url = f"{SHARE_PREFIX}{token}/{entry['id']}"
            name = html.escape(entry["name"])
            preview = ""
            if entry["kind"].startswith("image/"):
                preview = f'<img src="{url}?view=1" alt="{name}" loading="lazy">'
            elif entry["kind"].startswith("video/"):
                preview = (
                    f'<video src="{url}?view=1" controls preload="metadata"></video>'
                )
            items.append(
                f'<li>{preview}<div class="row"><span class="name">{name}</span>'
                f'<span class="size">{size_label(entry["size"])}</span>'
                f'<a class="button" href="{url}" download>Скачать</a></div></li>'
            )

        page = (
            TEMPLATE.read_text(encoding="utf-8")
            .replace("__PAGE_STYLES__", page_styles())
            .replace("__ITEMS__", "\n".join(items))
        )

        return web.Response(
            text=page,
            content_type="text/html",
            headers={**PRIVATE, "Content-Security-Policy": PAGE_POLICY},
        )

    async def _shared_file(self, request: web.Request) -> web.StreamResponse:
        found = self._plugin.shared(
            request.match_info["token"], int(request.match_info["id"])
        )

        if not found:
            return self._miss(request)

        entry = found[0]

        return self._send(
            entry, inline=request.query.get("view") == "1" and bool(entry["kind"])
        )

    def _miss(self, request: web.Request) -> web.Response:
        if not self._rate.allow(rate_key(request, "files-miss"), MISSES_PER_HOUR, HOUR):
            return web.Response(status=429, text="Слишком много запросов.")

        return web.Response(
            text=render_not_found(),
            content_type="text/html",
            status=404,
            headers=PRIVATE,
        )

    def _send(self, entry: dict, inline: bool) -> web.StreamResponse:
        return web.FileResponse(
            self._plugin.path(entry["stored"]),
            headers={
                **PRIVATE,
                "Content-Type": entry["kind"] if inline else "application/octet-stream",
                "Content-Disposition": disposition(
                    "inline" if inline else "attachment", entry["name"]
                ),
                "Content-Security-Policy": FILE_POLICY,
            },
        )
