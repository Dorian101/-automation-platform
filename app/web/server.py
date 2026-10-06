import html
import logging
from collections.abc import Mapping
from pathlib import Path

from aiohttp import web

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
    verify_access,
)

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

DAY_SECONDS = 86400

# Only the login page is reachable without a session. Everything else falls
# back to the login form or a 401, so a route added later is protected by
# default rather than by remembering to protect it.
#
# /health is the exception: it has to answer while the database is down, and
# requiring a session would make it fail at authentication instead of reporting
# the failure. It still needs the proxy's shared secret and reveals nothing but
# a boolean.
PUBLIC_PATHS = {"/login", "/health"}


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

    def _render_login(
        self,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        source = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")

        error_block = f'<p class="error">{html.escape(error)}</p>' if error else ""

        body = (
            source.replace("__ERROR_BLOCK__", error_block).replace(
                "__USERNAME__", html.escape(username)
            )
        )

        return web.Response(text=body, content_type="text/html", status=status)

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
