import hmac
import html
import logging
import secrets
from datetime import time

from aiohttp import web
from psycopg.errors import UniqueViolation

from app.core.config import Config
from app.db.push_repo import BadSubscription, TooManyDevices
from app.db.sessions_repo import digest

from . import visitor as visit_details
from .auth import SESSION_COOKIE
from .common import TEMPLATES_DIR, error_block, form_value, rate_key
from .csrf import (
    CSRF_FIELD,
    FORGED_FORM_MESSAGE,
)
from .pages import menu_styles
from .responses import redirect as _redirect
from .responses import token_rejected
from .telegram_auth import TelegramAuthError, verify_login

logger = logging.getLogger(__name__)


# Binds a Login Widget payload to the browser that started it. The signature
# proves a real Telegram user signed it; this proves this browser is the one
# presenting it. Without it, anyone could take their own valid payload and ask
# someone else's browser to submit it, linking the victim's account to the
# attacker's chat and handing over the victim's notifications.
LINK_NONCE_COOKIE = "platform_link_nonce"


NONCE_BYTES = 24


# A payload that survives only minutes. Same reason as the signature's age
# limit: a nonce held longer is a nonce that can be replayed longer.
NONCE_TTL_SECONDS = 600


def _render_index(
    link,
    nonce: str,
    nav_html: str,
    error: str = "",
    csrf: str = "",
    devices: list | None = None,
    quiet=None,
) -> str:
    """Build the console, with the Telegram panel filled in for this account.

    The panel is the first part of the interface that renders state rather than
    the result of a command, which is why it is a separate argument and not
    another string substitution inline: the alternative is three unrelated
    `replace()` calls that have to be kept in step by hand.
    """
    source = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")

    source = source.replace("__NAV__", nav_html)
    source = source.replace("__MENU_STYLES__", menu_styles())
    source = source.replace("__TELEGRAM_BLOCK__", _telegram_panel(link, nonce, csrf))
    source = source.replace("__PUSH_BLOCK__", _push_panel(devices or [], csrf))
    source = source.replace("__QUIET_BLOCK__", _quiet_form(quiet, csrf))
    source = source.replace(
        "__ERROR_BLOCK__",
        error_block(error),
    )

    return source


def _push_panel(devices: list, csrf: str) -> str:
    if not (
        Config.VAPID_PUBLIC_KEY and Config.VAPID_PRIVATE_KEY and Config.VAPID_SUBJECT
    ):
        return '<p class="hint">Уведомления на этом сервере не настроены.</p>'

    rows = "".join(
        "<li>"
        f"{html.escape(device['label'])} · с {device['created_at']:%d.%m.%Y}"
        '<form method="post" action="/account/push/remove" class="inline">'
        f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
        f'<input type="hidden" name="id" value="{device["id"]}">'
        '<button type="submit" class="secondary">Отключить</button>'
        "</form></li>"
        for device in devices
    )
    listing = f'<ul class="devices">{rows}</ul>' if rows else ""
    test = (
        '<button type="button" id="push-test" class="secondary">Проверить</button>'
        if devices
        else ""
    )

    return (
        f'<div id="push" data-csrf="{html.escape(csrf)}" '
        f'data-key="{html.escape(Config.VAPID_PUBLIC_KEY)}">'
        '<p class="hint" id="push-state">Напоминания, сроки задач и сообщения '
        "автоматизаций приходят сюда, а в Telegram — только если устройств нет.</p>"
        '<button type="button" id="push-on">Включить на этом устройстве</button>'
        f"{test}{listing}</div>"
        '<script src="/push.js" defer></script>'
    )


def _quiet_form(quiet, csrf: str) -> str:
    start, end = (f"{one:%H:%M}" for one in quiet) if quiet else ("", "")
    state = (
        f"Сейчас: с {start} до {end}, утром приходит всё накопленное."
        if quiet
        else "Выключены: уведомления приходят в любое время."
    )
    off = (
        '<button type="submit" name="off" value="1" class="secondary">'
        "Выключить</button>"
        if quiet
        else ""
    )

    return (
        f'<p class="hint">{state}</p>'
        '<form method="post" action="/account/quiet" class="quiet">'
        f'<input type="hidden" name="{CSRF_FIELD}" value="{html.escape(csrf)}">'
        f'<label>с <input type="time" name="from" value="{start or "23:00"}" '
        "required></label>"
        f'<label>до <input type="time" name="to" value="{end or "08:00"}" '
        "required></label>"
        f'<button type="submit">Сохранить</button>{off}</form>'
    )


def _telegram_panel(link, nonce: str, csrf: str) -> str:
    """Offer exactly one of the two things the account can do about Telegram."""
    if not telegram_linking_enabled():
        return (
            '<p class="hint">Привязка Telegram на этом сервере не настроена.</p>'
        )

    if link is not None:
        return (
            f'<p class="hint">Напоминания приходят в Telegram-чат '
            f"<b>{html.escape(str(link.telegram_id))}</b>. Заметки и буфер обмена "
            "доступны с обеих сторон.</p>"
            '<form method="post" action="/account/telegram/unlink">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
            '<button type="submit" class="secondary">Отвязать Telegram</button>'
            "</form>"
        )

    # The widget script comes from telegram.org and replaces its own element
    # with Telegram's login button, so the bot username and the return URL
    # live on the <script> tag itself. The widget only looks at
    # script[data-telegram-login]; putting those attributes on another
    # element would leave it untouched and the account with nothing to click.
    # The nonce rides along so the reply can be tied to the browser that
    # asked.
    return (
        '<p class="hint">Привяжите Telegram, чтобы напоминания, созданные '
        "здесь, приходили в бота.</p>"
        f'<script async src="https://telegram.org/js/telegram-widget.js?22" '
        f'data-telegram-login="{html.escape(Config.TELEGRAM_BOT_USERNAME)}" '
        f'data-size="large" '
        f'data-auth-url="/account/telegram/link?nonce={nonce}">'
        "</script>"
    )


def _new_nonce() -> str:
    return secrets.token_urlsafe(NONCE_BYTES)


def _set_nonce_cookie(response: web.Response, nonce: str) -> None:
    response.set_cookie(
        LINK_NONCE_COOKIE,
        nonce,
        max_age=NONCE_TTL_SECONDS,
        path="/",
        httponly=True,
        # Lax rather than Strict: the widget redirects back from telegram.org,
        # which is a top-level GET, and Strict would withhold the cookie from
        # exactly the navigation this exists to allow.
        samesite="lax",
        secure=Config.SESSION_COOKIE_SECURE,
    )


def _clear_nonce(response: web.Response) -> web.Response:
    """Spend the nonce, so the same URL cannot be tried a second time.

    Sending the expiry is what does it: the browser drops the cookie, so the
    replay arrives with nothing to compare against.
    """
    response.del_cookie(LINK_NONCE_COOKIE, path="/")

    return response


def _nonce_matches(nonce: str, request: web.Request) -> bool:
    """Whether the payload came back from the browser that asked.

    Only a match is accepted; spending the nonce is `_clear_nonce`'s job,
    because a request may fail later for an unrelated reason and must not burn
    a nonce the person never had the chance to fix.
    """
    stored = request.cookies.get(LINK_NONCE_COOKIE, "")

    return bool(stored) and bool(nonce) and hmac.compare_digest(stored, nonce)


def telegram_linking_enabled() -> bool:
    """Whether this deployment can offer Telegram linking at all.

    Both a bot token and a username are needed: the widget renders without a
    username and the signature cannot be checked without a token. Requiring
    both means a half-configured deployment shows nothing rather than a button
    that fails on click.
    """
    return bool(Config.TELEGRAM_BOT_USERNAME.strip() and Config.BOT_TOKEN)


class AccountRoutes:
    async def _index(self, request: web.Request) -> web.Response:
        user = self._current_user(request)

        nonce = ""
        if telegram_linking_enabled() and self._links.get_by_user_id(user.id) is None:
            nonce = _new_nonce()

        session_token = request.cookies.get(SESSION_COOKIE, "")
        csrf = self._csrf.session_token(session_token) if session_token else ""

        body = _render_index(
            self._links.get_by_user_id(user.id) if user else None,
            nonce,
            self._nav(request),
            csrf=csrf,
            devices=self._push.devices(user.id) if user else [],
            quiet=self._users.quiet_hours(user.id) if user else None,
        )

        response = web.Response(text=body, content_type="text/html")

        if nonce:
            _set_nonce_cookie(response, nonce)

        return response

    async def _telegram_link(self, request: web.Request) -> web.Response:
        """Link the signed-in account to the Telegram user in the payload."""
        if not telegram_linking_enabled():
            return web.Response(status=404)

        user = self._current_user(request)

        if user is None:
            return _redirect("/login")

        payload = dict(request.query)

        csrf = self._csrf.session_token(request.cookies.get(SESSION_COOKIE, ""))

        response: web.Response

        if not _nonce_matches(payload.pop("nonce", ""), request):
            # The nonce is spent either way, so a replayed URL finds nothing to
            # match even while its signature is still inside the age limit.
            response = self._link_result(
                user,
                "Telegram sign-in expired. Try again.",
                self._nav(request),
                csrf=csrf,
            )
            return _clear_nonce(response)

        try:
            telegram_user = verify_login(payload, Config.BOT_TOKEN)
        except TelegramAuthError:
            logger.warning("Rejected a Telegram login payload")
            response = self._link_result(
                user,
                "Could not verify that Telegram sign-in.",
                self._nav(request),
                csrf=csrf,
            )
            return _clear_nonce(response)

        existing = self._links.get_by_user_id(user.id)

        if existing is not None and existing.telegram_id == telegram_user.id:
            # Already exactly this chat. Pressing the button on a page held
            # open since before linking is not an error, and reporting one
            # would suggest the link is broken when it is not.
            return _clear_nonce(_redirect("/"))

        if existing is not None:
            # Said out loud in the log rather than swapped quietly. Re-linking
            # is how a stale link gets corrected, and doing it invisibly would
            # leave notifications pointing at a chat nobody expects any more.
            self._links.unlink(user.id)
            logger.info(
                "Replaced Telegram link for %s: %s -> %s",
                user.username,
                existing.telegram_id,
                telegram_user.id,
            )

        try:
            self._links.link(user.id, telegram_user.id)
        except UniqueViolation:
            # Someone else's account holds this chat. Both sides are named:
            # which of the two collided is not something the caller can infer
            # from "cannot link".
            owner = self._links.get_by_telegram_id(telegram_user.id)
            owner_user = owner and self._users.get_by_id(owner.user_id)
            name = owner_user.username if owner_user else "another account"

            return self._link_result(
                user,
                f"Этот Telegram-аккаунт уже привязан к {name}.",
                self._nav(request),
                csrf=csrf,
            )

        logger.info(
            "Linked web account %s to Telegram chat %s",
            user.username,
            telegram_user.id,
        )

        # Consumed on success too, so the button cannot be pressed twice with
        # two different chats by keeping the page open.
        return _clear_nonce(_redirect("/"))

    async def _telegram_unlink(self, request: web.Request) -> web.Response:
        form = await request.post()

        user = self._current_user(request)

        if user is None:
            return _redirect("/login")

        if not self._csrf.accepts(
            request.cookies.get(SESSION_COOKIE, ""),
            form_value(form, CSRF_FIELD),
        ):
            return token_rejected(request, "/")

        existing = self._links.get_by_user_id(user.id)

        if existing is not None:
            self._links.unlink(user.id)
            logger.info(
                "Unlinked web account %s from Telegram chat %s",
                user.username,
                existing.telegram_id,
            )

        return _redirect("/")

    def _link_result(
        self,
        user,
        message: str,
        nav_html: str,
        csrf: str = "",
    ) -> web.Response:
        """Report a failed link on the page it was started from.

        A redirect would put the message in the URL, and nothing on the index
        page reads its query string.
        """
        body = _render_index(
            self._links.get_by_user_id(user.id),
            "",
            nav_html,
            message,
            csrf=csrf,
        )

        return web.Response(text=body, content_type="text/html", status=400)

    async def _push_request(self, request: web.Request):
        user = self._current_user(request)

        if user is None:
            refused = web.json_response({"error": "Войдите заново"}, status=401)
            return None, None, refused

        try:
            payload = await request.json()
        except Exception:
            payload = None

        token = request.cookies.get(SESSION_COOKIE, "")

        if not isinstance(payload, dict) or not self._csrf.accepts(
            token, payload.get("csrf") if isinstance(payload.get("csrf"), str) else ""
        ):
            return None, None, web.json_response(
                {"error": FORGED_FORM_MESSAGE}, status=403
            )

        return user, payload, None

    async def _push_subscribe(self, request: web.Request) -> web.Response:
        user, payload, refused = await self._push_request(request)

        if refused is not None:
            return refused

        label = visit_details.describe(request.headers.get("User-Agent", ""))

        try:
            self._push.add(
                user.id,
                payload.get("subscription"),
                label,
                digest(request.cookies.get(SESSION_COOKIE, "")),
            )
        except BadSubscription:
            return web.json_response(
                {"error": "Браузер прислал неподходящую подписку"}, status=400
            )
        except TooManyDevices:
            return web.json_response(
                {"error": "Слишком много устройств. Отключите ненужные."}, status=400
            )

        return web.json_response({"ok": True})

    async def _push_unsubscribe(self, request: web.Request) -> web.Response:
        user, payload, refused = await self._push_request(request)

        if refused is not None:
            return refused

        endpoint = payload.get("endpoint")

        if isinstance(endpoint, str):
            self._push.remove_endpoint(user.id, endpoint)

        return web.json_response({"ok": True})

    async def _push_test(self, request: web.Request) -> web.Response:
        user, _, refused = await self._push_request(request)

        if refused is not None:
            return refused

        if not self._rate.allow(rate_key(request, "push-test"), 5, 600):
            return web.json_response({"error": "Слишком часто"}, status=429)

        identity = self._users.identity_for(user)

        if not self._push.subscriptions(identity) or self._notifier is None:
            return web.json_response(
                {"error": "Нет устройств с уведомлениями"}, status=400
            )

        delivered = await self._notifier.deliver(
            identity, "Проверка: уведомления работают.", urgent=True
        )

        return web.json_response(
            {"text": "Отправлено на ваши устройства." if delivered else "Не дошло."}
        )

    async def _push_remove(self, request: web.Request) -> web.Response:
        form = await request.post()
        token = request.cookies.get(SESSION_COOKIE, "")

        if not self._csrf.accepts(token, form_value(form, CSRF_FIELD)):
            return token_rejected(request, "/")

        user = self._current_user(request)
        device = form_value(form, "id")

        if user is not None and device.isdigit():
            self._push.remove(user.id, int(device))

        return _redirect("/")

    async def _quiet_hours(self, request: web.Request) -> web.Response:
        form = await request.post()

        if not self._csrf.accepts(
            request.cookies.get(SESSION_COOKIE, ""), form_value(form, CSRF_FIELD)
        ):
            return token_rejected(request, "/")

        user = self._current_user(request)

        if user is None:
            return _redirect("/login")

        if form_value(form, "off"):
            self._users.set_quiet_hours(user.id, None, None)
            return _redirect("/")

        try:
            start = time.fromisoformat(form_value(form, "from"))
            end = time.fromisoformat(form_value(form, "to"))
        except ValueError:
            return _redirect("/")

        self._users.set_quiet_hours(
            user.id, start.replace(second=0, microsecond=0),
            end.replace(second=0, microsecond=0),
        )

        return _redirect("/")
