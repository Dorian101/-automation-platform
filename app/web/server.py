import hmac
import html
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
        self._runner: web.AppRunner | None = None

    async def _index(self, request: web.Request) -> web.Response:
        identity = self._identity(request)
        user = self._current_user(request)

        nonce = ""
        if telegram_linking_enabled() and self._links.get_by_user_id(user.id) is None:
            nonce = _new_nonce()

        body = _render_index(
            identity.id if identity else "",
            self._links.get_by_user_id(user.id) if user else None,
            nonce,
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

        response: web.Response

        if not _nonce_matches(payload.pop("nonce", ""), request):
            # The nonce is spent either way, so a replayed URL finds nothing to
            # match even while its signature is still inside the age limit.
            response = self._link_result(user, "Telegram sign-in expired. Try again.")
            return _clear_nonce(response)

        try:
            telegram_user = verify_login(payload, Config.BOT_TOKEN)
        except TelegramAuthError:
            logger.warning("Rejected a Telegram login payload")
            response = self._link_result(
                user,
                "Could not verify that Telegram sign-in.",
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
            )

        # Consumed on success too, so the button cannot be pressed twice with
        # two different chats by keeping the page open.
        return _clear_nonce(_redirect("/"))

    async def _telegram_unlink(self, request: web.Request) -> web.Response:
        user = self._current_user(request)

        if user is None:
            return _redirect("/login")

        self._links.unlink(user.id)

        return _redirect("/")

    def _link_result(self, user, message: str) -> web.Response:
        """Report a failed link on the page it was started from.

        A redirect would put the message in the URL, and nothing on the index
        page reads its query string.
        """
        body = _render_index(
            user.username,
            self._links.get_by_user_id(user.id),
            "",
            message,
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

        return web.json_response({"result": result.text})

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

        user = self._users.authenticate(username, password)

        if user is None:
            # The message does not say which half was wrong: an unknown
            # username and a wrong password must look the same from outside.
            return self._render_login(
                error="Incorrect username or password.",
                username=username,
                status=401,
            )

        token = self._sessions.create(user.id, Config.SESSION_TTL_DAYS * DAY_SECONDS)

        response = _redirect("/")
        set_session_cookie(response, token)
        return response

    async def _logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(SESSION_COOKIE, "")

        if token:
            self._sessions.delete(token)

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
        return response

    def _render_login(
        self,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        return _render_card(
            "login.html",
            {
                "__ERROR_BLOCK__": _error_block(error),
                "__USERNAME__": html.escape(username),
                "__SIGNUP_BLOCK__": _signup_link(),
            },
            status=status,
        )

    def _render_signup(
        self,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        return _render_card(
            "signup.html",
            {
                "__ERROR_BLOCK__": _error_block(error),
                "__USERNAME__": html.escape(username),
            },
            status=status,
        )

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
                web.get("/health", self._health),
            ],
        )
        return app

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


def _render_index(
    username: str,
    link,
    nonce: str,
    error: str = "",
) -> str:
    """Build the console, with the Telegram panel filled in for this account.

    The panel is the first part of the interface that renders state rather than
    the result of a command, which is why it is a separate argument and not
    another string substitution inline: the alternative is three unrelated
    `replace()` calls that have to be kept in step by hand.
    """
    source = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")

    source = source.replace("__USER__", html.escape(username))
    source = source.replace("__TELEGRAM_BLOCK__", _telegram_panel(link, nonce))
    source = source.replace(
        "__ERROR_BLOCK__",
        f'<p class="error">{html.escape(error)}</p>' if error else "",
    )

    return source


def _telegram_panel(link, nonce: str) -> str:
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
