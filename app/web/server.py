import html
import logging
from collections.abc import Mapping
from pathlib import Path

from aiohttp import web
from psycopg.errors import UniqueViolation

from app.core.config import Config
from app.core.identity import Identity
from app.core.results import CommandError
from app.db.database import Database
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

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

DAY_SECONDS = 86400

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
        self._runner: web.AppRunner | None = None

    async def _index(self, request: web.Request) -> web.Response:
        identity = self._identity(request)

        source = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
        body = source.replace("__USER__", html.escape(identity.id if identity else ""))

        return web.Response(text=body, content_type="text/html")

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
