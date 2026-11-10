import html
import json

from aiohttp import web

from app.core.results import CommandError

from .auth import SESSION_COOKIE
from .common import (
    CSP_NONCE_PLACEHOLDER,
    error_block,
    form_value,
    rate_key,
    render_card,
)
from .csrf import (
    CSRF_FIELD,
    FORGED_FORM_MESSAGE,
)
from .documents import PUBLIC_BASE_URL

LINKS_API_PATH = "/api/links"


LINKS_SAVE_PATH = "/links/save"


LINKS_PER_WINDOW = 60


LINKS_BAD_KEYS = 10


LINKS_WINDOW_SECONDS = 600


LINKS_MAX_BODY = 8192


def _links_reply(text: str, status: int) -> web.Response:
    return web.json_response({"text": text}, status=status)


def _render_save_link(body: str, script: str = "", status: int = 200) -> web.Response:
    return render_card(
        "save_link.html", {"__BODY__": body, "__SCRIPT__": script}, status=status
    )


def _bookmarklet_block(request: web.Request) -> str:
    origin = PUBLIC_BASE_URL or f"{request.scheme}://{request.host}"
    target = json.dumps(f"{origin}{LINKS_SAVE_PATH}?url=")
    code = (
        "javascript:(function(){window.open("
        f"{target}+encodeURIComponent(location.href),"
        "'save-link','width=480,height=360');})()"
    )

    return (
        '<p class="sub">Перетащите кнопку на панель закладок браузера. '
        "На любой странице нажмите её, и ссылка на страницу сохранится.</p>"
        f'<p><a class="bookmarklet" href="{html.escape(code)}">В ссылки</a></p>'
    )


class LinkRoutes:
    async def _api_links(self, request: web.Request) -> web.Response:
        if not self._rate.allow(
            rate_key(request, "links-api"), LINKS_PER_WINDOW, LINKS_WINDOW_SECONDS
        ):
            return _links_reply("Слишком часто", 429)

        scheme, _, key = request.headers.get("Authorization", "").partition(" ")
        key = key.strip()

        if scheme.lower() != "bearer" or not key:
            return _links_reply("Нужен ключ", 401)

        if request.content_length is None or request.content_length > LINKS_MAX_BODY:
            return _links_reply("Слишком длинный запрос", 413)

        identity = self._keys.identity_for(key)

        if identity is None:
            if not self._rate.allow(
                rate_key(request, "links-api-bad"),
                LINKS_BAD_KEYS,
                LINKS_WINDOW_SECONDS,
            ):
                return _links_reply("Слишком часто", 429)

            return _links_reply("Ключ не подошёл", 401)

        try:
            payload = await request.json()
        except Exception:
            payload = None

        if not isinstance(payload, dict):
            return _links_reply("Нужен JSON со ссылкой", 400)

        url = payload.get("url")
        text = payload.get("text", "")

        if not isinstance(url, str) or not url.strip() or not isinstance(text, str):
            return _links_reply("Нужна ссылка", 400)

        try:
            result = await self._manager.save_links(identity, [url.strip()], text)
        except CommandError as error:
            return _links_reply(str(error), 400)

        return _links_reply(result.text, 200)

    async def _save_link_form(self, request: web.Request) -> web.Response:
        url = request.query.get("url", "").strip()

        if not url:
            return _render_save_link(_bookmarklet_block(request))

        token = request.cookies.get(SESSION_COOKIE, "")
        shown = html.escape(url[:2000])

        return _render_save_link(
            f'<form method="post" action="{LINKS_SAVE_PATH}">'
            f'<input type="hidden" name="{CSRF_FIELD}" '
            f'value="{html.escape(self._csrf.session_token(token))}">'
            f'<input type="hidden" name="url" value="{shown}">'
            f'<p class="sub url">{shown}</p>'
            '<label>Описание<input type="text" name="text" maxlength="1000" '
            'autocomplete="off" autofocus></label>'
            '<button type="submit">Сохранить</button>'
            "</form>"
        )

    async def _save_link_submit(self, request: web.Request) -> web.Response:
        form = await request.post()
        token = request.cookies.get(SESSION_COOKIE, "")

        if not self._csrf.accepts(token, form_value(form, CSRF_FIELD)):
            return _render_save_link(error_block(FORGED_FORM_MESSAGE), status=403)

        identity = self._identity(request)
        url = form_value(form, "url").strip()

        try:
            result = await self._manager.save_links(
                identity, [url], form_value(form, "text")
            )
        except CommandError as error:
            return _render_save_link(error_block(str(error)), status=400)

        return _render_save_link(
            f'<p class="sub">{html.escape(result.text)}</p>',
            script=f'<script nonce="{CSP_NONCE_PLACEHOLDER}">'
            "setTimeout(function(){window.close();},1500);</script>",
        )
