import hmac
import html
import json
import logging
import secrets
from collections.abc import Mapping
from pathlib import Path

from aiohttp import web
from psycopg.errors import UniqueViolation

from app.core.config import Config
from app.core.identity import Identity
from app.core.results import CommandError
from app.db.database import Database
from app.db.links_repo import TelegramLinksRepository
from app.db.sessions_repo import SessionsRepository
from app.db.users_repo import UsersRepository
from app.plugins.manager import PluginManager

from .auth import (
    SESSION_COOKIE,
    clear_session_cookie,
    resolve_identity,
    set_session_cookie,
    signup_allowed,
    signup_enabled,
    verify_access,
)
from .csrf import (
    CSRF_COOKIE,
    CSRF_FIELD,
    CSRF_TTL_SECONDS,
    FORGED_FORM_MESSAGE,
    CsrfSigner,
    has_valid_cookie,
    new_token,
)
from .ratelimit import RateLimiter
from .telegram_auth import TelegramAuthError, verify_login

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

DAY_SECONDS = 86400

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

# Only pages that make sense without a session are reachable without one.
# Everything else falls back to the login form or a 401, so a route added
# later is protected by default rather than by remembering to protect it.
#
# /health is the exception: it has to answer while the database is down, and
# requiring a session would make it fail at authentication instead of
# reporting the failure. It still needs the proxy's shared secret and reveals
# nothing but a boolean.
#
# /about and /project are static documents: they read nothing from the
# database and render no user data, which is the only reason they belong
# here. Anything added to them later has to keep that property, or the page
# has to leave this set.
#
# /signup is listed even though it answers 404 when no invite code is
# configured: it is the page itself that decides, so that turning sign-up off
# cannot leave a route behind that still needs a session to reach.
PUBLIC_PATHS = {"/login", "/signup", "/about", "/project", "/health"}

# Sent on every response, so no page or JSON answer can ever forget one. CSP
# allows the Telegram Login Widget's script from telegram.org and inline
# scripts and styles, which the console relies on; everything else stays
# locked to the app's own origin. form-action 'self' is what keeps a stray
# <form> from shipping a session cookie somewhere it does not belong.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


class WebServer:
    def __init__(
        self,
        manager: PluginManager,
        database: Database,
        host: str,
        port: int,
    ):
        self._manager = manager
        self._database = database
        self._host = host
        self._port = port
        self._users = UsersRepository(database)
        self._sessions = SessionsRepository(database)
        self._links = TelegramLinksRepository(database)
        self._rate = RateLimiter()
        self._csrf = CsrfSigner()
        self._runner: web.AppRunner | None = None

    async def _index(self, request: web.Request) -> web.Response:
        identity = self._identity(request)
        user = self._current_user(request)

        nonce = ""
        if telegram_linking_enabled() and self._links.get_by_user_id(user.id) is None:
            nonce = _new_nonce()

        session_token = request.cookies.get(SESSION_COOKIE, "")
        csrf = self._csrf.session_token(session_token) if session_token else ""

        body = _render_index(
            identity.id if identity else "",
            self._links.get_by_user_id(user.id) if user else None,
            nonce,
            csrf=csrf,
        )

        response = web.Response(text=body, content_type="text/html")

        if nonce:
            _set_nonce_cookie(response, nonce)

        return response

    async def _plugin_page(self, request: web.Request) -> web.Response:
        """A plugin's own page, rendered from the description it returned.

        The plugin answers with a dict; nothing here knows what a plugin
        shows. A route only exists for a plugin that declared it, so an
        unknown path falls through to the console rather than reaching a
        handler that has no owner.
        """
        plugin = self._manager.find_by_page(request.path)

        if plugin is None:
            return _redirect("/")

        identity = self._identity(request)

        if identity is None:
            return self._login_required(request)

        try:
            view = await plugin.page_view(identity)
        except CommandError as error:
            return web.Response(text=str(error), content_type="text/plain", status=400)
        except Exception:
            logger.exception("Page %s failed to build", plugin.name)
            return web.Response(
                text="Internal error",
                content_type="text/plain",
                status=500,
            )

        return _render_plugin_page(plugin.name, view)

    async def _plugin_page_action(self, request: web.Request) -> web.Response:
        """Something was done on a plugin page; answer with it redrawn.

        Every value arrives as a string and is converted by the plugin, which
        is the only place that knows what a valid amount or a valid month is.
        The target page is named in the body rather than in the path, because
        the action endpoint is one shared route.
        """
        identity = self._identity(request)

        if identity is None:
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Request body must be JSON"},
                status=400,
            )

        if not isinstance(payload, dict):
            return web.json_response(
                {"error": "Request body must be a JSON object"},
                status=400,
            )

        page = payload.get("page", "")
        action = payload.get("action", "")
        body = payload.get("payload", {})

        if not isinstance(page, str) or not isinstance(action, str):
            return web.json_response(
                {"error": "'page' and 'action' must be strings"},
                status=400,
            )

        if not isinstance(body, dict):
            return web.json_response(
                {"error": "'payload' must be an object"},
                status=400,
            )

        plugin = self._manager.find_by_page(page)

        if plugin is None:
            return web.json_response({"error": "Unknown page"}, status=404)

        try:
            view = await plugin.page_action(identity, action, body)
        except CommandError as error:
            return web.json_response({"error": str(error)}, status=400)
        except Exception:
            logger.exception("Action %s failed on %s", action, plugin.name)
            return web.json_response({"error": "Internal error"}, status=500)

        return web.json_response({"view": view})

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
                user, "Telegram sign-in expired. Try again.", csrf=csrf
            )
            return _clear_nonce(response)

        try:
            telegram_user = verify_login(payload, Config.BOT_TOKEN)
        except TelegramAuthError:
            logger.warning("Rejected a Telegram login payload")
            response = self._link_result(
                user,
                "Could not verify that Telegram sign-in.",
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
                f"That Telegram account is already linked to {name}.",
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
            _form_value(form, CSRF_FIELD),
        ):
            return _form_forged()

        existing = self._links.get_by_user_id(user.id)

        if existing is not None:
            self._links.unlink(user.id)
            logger.info(
                "Unlinked web account %s from Telegram chat %s",
                user.username,
                existing.telegram_id,
            )

        return _redirect("/")

    def _link_result(self, user, message: str, csrf: str = "") -> web.Response:
        """Report a failed link on the page it was started from.

        A redirect would put the message in the URL, and nothing on the index
        page reads its query string.
        """
        body = _render_index(
            user.username,
            self._links.get_by_user_id(user.id),
            "",
            message,
            csrf=csrf,
        )

        return web.Response(text=body, content_type="text/html", status=400)

    def _current_user(self, request: web.Request):
        """The account behind the session, or None.

        Separate from `_identity` because linking needs the row itself: the
        identity carries a username and no id, and the link is keyed by id.
        """
        token = request.cookies.get(SESSION_COOKIE, "")

        if not token:
            return None

        user_id = self._sessions.get_user_id(token)

        if user_id is None:
            return None

        user = self._users.get_by_id(user_id)

        if user is None or not user.is_active:
            return None

        return user

    async def _about(self, request: web.Request) -> web.Response:
        return _render_page("about.html")

    async def _project(self, request: web.Request) -> web.Response:
        return _render_page("project.html")

    async def _commands(self, request: web.Request) -> web.Response:
        plugins = [
            {
                "name": plugin.name,
                "version": plugin.version,
                "description": plugin.description,
                "commands": plugin.commands,
            }
            for plugin in self._manager.get_plugins()
        ]

        return web.json_response({"plugins": plugins})

    async def _execute(self, request: web.Request) -> web.Response:
        identity = self._identity(request)

        if identity is None:
            return self._login_required(request)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Request body must be JSON"},
                status=400,
            )

        command = payload.get("command", "")
        args = payload.get("args", "")

        if not isinstance(command, str) or not isinstance(args, str):
            return web.json_response(
                {"error": "'command' and 'args' must be strings"},
                status=400,
            )

        try:
            result = await self._manager.execute(command.strip(), args, identity)
        except CommandError as error:
            return web.json_response({"error": str(error)}, status=400)
        except Exception:
            logger.exception("Command %s failed", command)
            return web.json_response(
                {"error": "Internal error"},
                status=500,
            )

        # data is included only when the plugin produced it, so a response
        # from a plugin that has nothing structured to say keeps the exact
        # shape it had before the field existed.
        body = {"result": result.text}

        if result.data is not None:
            body["data"] = result.data

        return web.json_response(body)

    async def _health(self, request: web.Request) -> web.Response:
        try:
            with self._database.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
        except Exception:
            logger.exception("Health check failed")
            return web.json_response({"status": "error", "database": False}, status=503)

        return web.json_response({"status": "ok", "database": True})

    async def _login_form(self, request: web.Request) -> web.Response:
        if self._identity(request) is not None:
            return _redirect("/")

        return self._render_login()

    async def _login_submit(self, request: web.Request) -> web.Response:
        form = await request.post()

        username = _form_value(form, "username")
        password = _form_value(form, "password")

        if not has_valid_cookie(
            request.cookies.get(CSRF_COOKIE, ""),
            _form_value(form, CSRF_FIELD),
        ):
            # Rejected before any scrypt work: a forged submission is not a
            # real login attempt, and should not cost one.
            logger.warning(
                "Rejected a login without a valid CSRF token from %s",
                _client_ip(request),
            )
            return self._render_login(
                error=FORGED_FORM_MESSAGE,
                username=username,
                status=400,
            )

        if not self._rate.allow(
            _rate_key(request, "login", username),
            Config.LOGIN_ATTEMPTS_PER_WINDOW,
            Config.RATE_LIMIT_WINDOW_SECONDS,
        ):
            logger.warning(
                "Login rate limit reached for %r from %s",
                username,
                _client_ip(request),
            )
            return self._render_login(
                error="Too many attempts. Try again later.",
                username=username,
                status=429,
            )

        user = self._users.authenticate(username, password)

        if user is None:
            # The message does not say which half was wrong: an unknown
            # username and a wrong password must look the same from outside.
            # The username is written with %r because it is caller-controlled
            # input and must not be able to forge a log line.
            logger.warning(
                "Failed login attempt for %r from %s", username, _client_ip(request)
            )
            return self._render_login(
                error="Incorrect username or password.",
                username=username,
                status=401,
            )

        # A correct password after a run of typos reloads the allowance rather
        # than starting the next window from the typos' count.
        self._rate.clear(_rate_key(request, "login", username))

        token = self._sessions.create(user.id, Config.SESSION_TTL_DAYS * DAY_SECONDS)

        response = _redirect("/")
        set_session_cookie(response, token)
        # The anonymous token has done its job; the session-backed forms carry
        # their own token derived from the session.
        response.del_cookie(CSRF_COOKIE, path="/")

        logger.info("Signed in %r from %s", user.username, _client_ip(request))

        return response

    async def _logout(self, request: web.Request) -> web.Response:
        form = await request.post()

        token = request.cookies.get(SESSION_COOKIE, "")

        if not token:
            # No session to end and nothing that was asked of the cookie; the
            # repeated logout just bounces. There is no state change here, so
            # no CSRF check stands between a guest and a redirect.
            response = _redirect("/login")
            clear_session_cookie(response)
            return response

        if not self._csrf.accepts(token, _form_value(form, CSRF_FIELD)):
            return _form_forged()

        identity = self._identity(request)

        self._sessions.delete(token)

        logger.info("Signed out %r", identity.id if identity else "unknown session")

        response = _redirect("/login")
        clear_session_cookie(response)
        return response

    async def _signup_form(self, request: web.Request) -> web.Response:
        # 404 rather than 403: with no invite code configured this deployment
        # has no sign-up page, and the path should behave like any other that
        # does not exist.
        if not signup_enabled():
            return web.Response(status=404)

        if self._identity(request) is not None:
            return _redirect("/")

        return self._render_signup()

    async def _signup_submit(self, request: web.Request) -> web.Response:
        if not signup_enabled():
            return web.Response(status=404)

        # Same as the GET: an existing session must not be quietly swapped for
        # the account someone is about to create. Signing out first is the
        # one step that makes that swap deliberate.
        if self._identity(request) is not None:
            return _redirect("/")

        form = await request.post()

        invite = _form_value(form, "invite")
        username = _form_value(form, "username")
        password = _form_value(form, "password")

        if not has_valid_cookie(
            request.cookies.get(CSRF_COOKIE, ""),
            _form_value(form, CSRF_FIELD),
        ):
            logger.warning(
                "Rejected a sign-up without a valid CSRF token from %s",
                _client_ip(request),
            )
            return self._render_signup(
                error=FORGED_FORM_MESSAGE,
                username=username,
                status=400,
            )

        if not self._rate.allow(
            _rate_key(request, "signup"),
            Config.SIGNUP_ATTEMPTS_PER_WINDOW,
            Config.SIGNUP_WINDOW_SECONDS,
        ):
            logger.warning("Sign-up rate limit reached from %s", _client_ip(request))
            return self._render_signup(
                error="Too many attempts. Try again later.",
                username=username,
                status=429,
            )

        if not signup_allowed(invite):
            return self._render_signup(
                error="Invalid invite code.",
                username=username,
                status=403,
            )

        try:
            user = self._users.create(username, password)
        except ValueError as error:
            # PasswordError is a ValueError, so a short password and an empty
            # username both arrive here carrying a message written for the
            # person filling in the form.
            return self._render_signup(
                error=str(error),
                username=username,
                status=400,
            )
        except UniqueViolation:
            return self._render_signup(
                error="That username is taken.",
                username=username,
                status=409,
            )

        # Straight into a session: making someone sign in again immediately
        # after registering would only be an extra round trip for a form they
        # have just proved they can fill in.
        token = self._sessions.create(
            user.id, Config.SESSION_TTL_DAYS * DAY_SECONDS
        )

        response = _redirect("/")
        set_session_cookie(response, token)
        response.del_cookie(CSRF_COOKIE, path="/")

        logger.info(
            "Account created for %r from %s", user.username, _client_ip(request)
        )

        return response

    def _render_login(
        self,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        token = new_token()

        response = _render_card(
            "login.html",
            {
                "__ERROR_BLOCK__": _error_block(error),
                "__USERNAME__": html.escape(username),
                "__SIGNUP_BLOCK__": _signup_link(),
                "__CSRF__": token,
            },
            status=status,
        )

        return _set_csrf_cookie(response, token)

    def _render_signup(
        self,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        token = new_token()

        response = _render_card(
            "signup.html",
            {
                "__ERROR_BLOCK__": _error_block(error),
                "__USERNAME__": html.escape(username),
                "__CSRF__": token,
            },
            status=status,
        )

        return _set_csrf_cookie(response, token)

    def _identity(self, request: web.Request) -> Identity | None:
        return resolve_identity(request, self._users, self._sessions)

    @staticmethod
    def _login_required(request: web.Request) -> web.Response:
        """Answer an unauthenticated request without handing over the page.

        Browsers are sent to the login form; API callers get a 401, because a
        redirect to HTML would only confuse them.
        """
        if request.path.startswith("/api/"):
            return web.json_response({"error": "Unauthorized"}, status=401)

        return _redirect("/login")

    @web.middleware
    async def _authenticate(self, request: web.Request, handler):
        denied = verify_access(request)

        if denied is not None:
            return denied

        if request.path in PUBLIC_PATHS:
            return await handler(request)

        if self._identity(request) is None:
            return self._login_required(request)

        return await handler(request)

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._authenticate])
        # A response_prepare hook runs for every response aiohttp sends, even
        # the one an exception turns into a 500 — middleware, running before
        # the handler, would not be reached when no response is ever built.
        app.on_response_prepare.append(_security_headers)
        app.add_routes(
            [
                web.get("/login", self._login_form),
                web.post("/login", self._login_submit),
                web.get("/signup", self._signup_form),
                web.post("/signup", self._signup_submit),
                web.get("/about", self._about),
                web.get("/project", self._project),
                web.post("/logout", self._logout),
                web.get("/account/telegram/link", self._telegram_link),
                web.post("/account/telegram/unlink", self._telegram_unlink),
                web.get("/", self._index),
                web.get("/api/commands", self._commands),
                web.post("/api/command", self._execute),
                web.get("/api/pages", self._pages),
                web.post("/api/page/action", self._plugin_page_action),
                web.get("/health", self._health),
            ],
        )

        # A page route is registered per plugin that declared one, so the
        # platform owns the path table and a plugin never adds a route to the
        # web layer by hand. The handler resolves the plugin from the path, so
        # it works the same whichever route reached it.
        for plugin in self._manager.get_plugins():
            if plugin.page:
                app.router.add_get(plugin.page, self._plugin_page)

        return app

    async def _pages(self, request: web.Request) -> web.Response:
        """Every plugin that has a page, so the console can link to them."""
        return web.json_response({"pages": self._manager.pages()})

    async def start(self) -> None:
        app = self.build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()

        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()

        logger.info("Web interface started on http://%s:%s", self._host, self._port)

    async def stop(self) -> None:
        if not self._runner:
            return

        await self._runner.cleanup()
        self._runner = None

        logger.info("Web interface stopped")


async def _security_headers(request: web.Request, response: web.Response) -> None:
    """Attach the security headers to everything leaving the app.

    HSTS is added only when the Secure flag is on, which the deployment keeps
    on for every TLS endpoint: on a plain-http prefix the header would be a
    promise nothing honours.
    """
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)

    if Config.SESSION_COOKIE_SECURE:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000"
        )


def _rate_key(request: web.Request, action: str, name: str = "") -> str:
    """Key an attempt, binding it to the address that made it.

    For login the username rides along too, so one person's typos do not lock
    out a whole network behind the proxy. Sign-ups are keyed by address alone:
    anyone registering is expected to hand out an invite code first.
    """
    return f"{action}|{_client_ip(request)}|{name.strip().lower()}"


def _client_ip(request: web.Request) -> str:
    """The caller's address for logging and rate limiting.

    Read from the proxy's header, not the socket: every request has already
    passed the X-Platform-Auth transport check by the time it gets here, so
    the header can only have been written by the reverse proxy.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")

    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.remote or "unknown"


def _render_index(
    username: str,
    link,
    nonce: str,
    error: str = "",
    csrf: str = "",
) -> str:
    """Build the console, with the Telegram panel filled in for this account.

    The panel is the first part of the interface that renders state rather than
    the result of a command, which is why it is a separate argument and not
    another string substitution inline: the alternative is three unrelated
    `replace()` calls that have to be kept in step by hand.
    """
    source = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")

    source = source.replace("__USER__", html.escape(username))
    source = source.replace("__CSRF__", html.escape(csrf))
    source = source.replace("__TELEGRAM_BLOCK__", _telegram_panel(link, nonce, csrf))
    source = source.replace(
        "__ERROR_BLOCK__",
        f'<p class="error">{html.escape(error)}</p>' if error else "",
    )

    return source


def _telegram_panel(link, nonce: str, csrf: str) -> str:
    """Offer exactly one of the two things the account can do about Telegram."""
    if not telegram_linking_enabled():
        return (
            '<p class="hint">Telegram linking is not configured on this '
            "deployment.</p>"
        )

    if link is not None:
        return (
            f'<p class="hint">Reminders reach Telegram chat '
            f"<b>{html.escape(str(link.telegram_id))}</b>. Notes and clipboard "
            "are shared both ways.</p>"
            '<form method="post" action="/account/telegram/unlink">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
            '<button type="submit" class="secondary">Unlink Telegram</button>'
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
        '<p class="hint">Link your Telegram account so reminders set here '
        "arrive in the bot.</p>"
        f'<script async src="https://telegram.org/js/telegram-widget.js?22" '
        f'data-telegram-login="{html.escape(Config.TELEGRAM_BOT_USERNAME)}" '
        f'data-size="large" '
        f'data-auth-url="/account/telegram/link?nonce={nonce}">'
        "</script>"
    )


def _set_csrf_cookie(response: web.Response, token: str) -> web.Response:
    """Tie the double-submit token to the browser that was handed the form.

    HttpOnly and SameSite=Lax, like the session cookie itself: the value is
    not for script to read, the page supplies it to echo back, and same-site
    is enough because the whole point is that a *cross*-site form does not get
    to carry cookies.

    Unlike the session cookie it is not marked Secure. The value is not a
    secret — the identical string sits in the page's HTML — so it needs only
    to survive the same request that carries the session, and letting it ride
    over plain http keeps the forms working on a deployment whose session
    cookie a browser would already refuse to send there.
    """
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=CSRF_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
    )

    return response


def _form_forged() -> web.Response:
    """Answer a session-backed POST that did not carry a valid token.

    Plain text rather than a re-render: unlike the anonymous forms there is no
    form on this page to refresh, and reloading takes the browser back to the
    console where a fresh token waits.
    """
    return web.Response(
        text=FORGED_FORM_MESSAGE,
        status=403,
        content_type="text/plain",
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


def _render_card(
    template: str,
    replacements: Mapping[str, str],
    status: int = 200,
) -> web.Response:
    """Build one of the card pages.

    Sign in and sign up are the same layout with different fields between the
    heading and the button, so the styles live in a file both templates pull
    in. A change to the card lands on both pages instead of one of them
    silently drifting.
    """
    source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    source = source.replace(
        "__STYLES__",
        (TEMPLATES_DIR / "_styles.html").read_text(encoding="utf-8"),
    )

    for name, value in replacements.items():
        source = source.replace(name, value)

    return web.Response(text=source, content_type="text/html", status=status)


def _render_page(template: str, status: int = 200) -> web.Response:
    """Render one of the public document pages.

    They take no arguments and read nothing: a page that has to work without
    a session must not quietly grow a dependency on one, so the only thing
    substituted here is the stylesheet. If a page starts needing data, that
    is the signal to reconsider whether it belongs in PUBLIC_PATHS at all.
    """
    source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    source = source.replace(
        "__PAGE_STYLES__",
        (TEMPLATES_DIR / "_page_styles.html").read_text(encoding="utf-8"),
    )

    return web.Response(text=source, content_type="text/html", status=status)


def _error_block(message: str) -> str:
    if not message:
        return ""

    return f'<p class="error">{html.escape(message)}</p>'


def _signup_link() -> str:
    """Offer registration exactly where it works.

    With no invite code configured there is no sign-up page either — it
    answers 404 — so showing the link would send people to a dead end.
    """
    if not signup_enabled():
        return ""

    return '<p class="alt">No account yet? <a href="/signup">Sign up</a></p>'


def _render_plugin_page(plugin_name: str, view: dict) -> web.Response:
    """Build a plugin's page: a shell plus its description, nothing else.

    The description is embedded as JSON and the browser paints it, so the
    first load and every redraw after an action go through one renderer. The
    server never draws a chart and the plugin never writes markup.

    ``</`` is escaped inside the JSON so a value containing it cannot close the
    script element; the block is ``type="application/json"``, which the browser
    treats as data and never executes.
    """
    source = (TEMPLATES_DIR / "_plugin_page.html").read_text(encoding="utf-8")

    payload = json.dumps(view, ensure_ascii=False).replace("</", "<\\/")

    source = source.replace(
        "__TITLE__",
        html.escape(str(view.get("title", plugin_name))),
    )
    source = source.replace("__PLUGIN__", html.escape(plugin_name))
    source = source.replace(
        "__STYLES__",
        (TEMPLATES_DIR / "_page_styles.html").read_text(encoding="utf-8"),
    )
    source = source.replace("__INITIAL_VIEW__", payload)

    return web.Response(text=source, content_type="text/html")


def _redirect(location: str) -> web.Response:
    """Send the browser to another path.

    A plain response rather than web.HTTPFound: returning an HTTPException
    from a handler is deprecated in aiohttp, and raising one would mean
    setting the session cookie on an exception object.
    """
    return web.Response(status=303, headers={"Location": location})


def _form_value(form: Mapping[str, object], name: str) -> str:
    """Read a text field, rejecting anything that is not a plain string.

    A file upload arrives as a FileField, and str() on it would quietly turn
    the request into a bogus login attempt instead of a clean rejection.
    """
    value = form.get(name, "")
    return value if isinstance(value, str) else ""
