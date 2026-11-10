import asyncio
import logging
import secrets
from urllib.parse import urlsplit

from aiohttp import web

from app.core.config import Config
from app.core.identity import Identity
from app.core.person import PersonResolver
from app.db.api_keys_repo import ApiKeysRepository
from app.db.database import Database
from app.db.feedback_repo import FeedbackRepository
from app.db.links_repo import TelegramLinksRepository
from app.db.push_repo import PushRepository
from app.db.sessions_repo import SessionsRepository
from app.db.settings_repo import SettingsRepository
from app.db.users_repo import UsersRepository
from app.db.visits_repo import VIEW, VisitsRepository
from app.plugins.manager import PluginManager

from . import i18n
from . import visitor as visit_details
from .account import AccountRoutes
from .admin import AdminArea
from .auth import (
    SESSION_COOKIE,
    resolve_identity,
    resolve_user,
    verify_access,
)
from .common import CSP_NONCE_PLACEHOLDER, TEMPLATES_DIR, leaves_no_trace
from .console import ConsoleRoutes
from .csrf import CsrfSigner
from .demo import VISITOR, DemoService, is_public_demo_path
from .documents import (
    APP_ASSETS,
    DOCUMENT_PATHS,
    LOCALIZED_DOCUMENTS,
    RELEASED_DOCUMENTS,
    DocumentRoutes,
    theme_head,
)
from .links import LINKS_API_PATH, LINKS_SAVE_PATH, LinkRoutes
from .live import LiveHub, notify_all
from .pages import (
    account_block,
    attic,
    language_switch,
    nav,
    render_not_found,
)
from .ratelimit import RateLimiter, client_ip
from .responses import redirect as _redirect
from .signin import SignInRoutes

logger = logging.getLogger(__name__)


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
# /feedback is the one public route that writes: the guest book's form posts
# there. Its limits are on the handler.
#
# /signup is listed even though it answers 404 when registration is closed:
# it is the page itself that decides, so that turning sign-up off cannot leave
# a route behind that still needs a session to reach.
#
# /robots.txt and /sitemap.xml are fetched by a crawler that has no session and
# cannot have one. Left out of this set they answered a redirect to the login
# form, which a search engine reads as "nothing to see here" — the pages it
# was meant to publish were not reachable from anywhere on the site, so it had
# no way to find them.
PUBLIC_PATHS = {
    "/login",
    "/signup",
    *LOCALIZED_DOCUMENTS,
    "/health",
    "/robots.txt",
    "/sitemap.xml",
    "/favicon.ico",
    "/favicon.svg",
    "/og-image.png",
    *APP_ASSETS,
    "/about-hero.js",
    "/about-page.js",
    "/project-page.js",
    "/project/trace",
    "/feedback",
    LINKS_API_PATH,
    "/push.js",
    "/sw.js",
    "/feedback.js",
}


def _content_security_policy(nonce: str) -> str:
    """The policy for one response.

    `script-src` carries a nonce rather than `unsafe-inline`. Every inline
    script on the site is template text, so a hash would read well enough — but
    the console's script holds the page path and the action URL, which change
    per plugin, so its content is not constant and cannot be hashed. A nonce
    does not care what the script contains, which is the only reason it works
    for that page and a hash would not.

    `style-src` keeps `unsafe-inline` on purpose. The stylesheets here are
    inlined into the document rather than served as files, an inline style
    attribute executes nothing, and narrowing it buys protection against an
    attack that cannot script the page in the first place.
    """
    script_sources = ["'self'", "https://telegram.org"]

    if nonce:
        script_sources.append(f"'nonce-{nonce}'")

    return (
        "default-src 'self'; "
        f"script-src {' '.join(script_sources)}; "
        # The Telegram widget does not draw a button on this page: it inserts
        # an iframe pointing at oauth.telegram.org. With no frame-src of its
        # own, that frame falls under `default-src 'self'` and is blocked,
        # which shows as an empty box where the sign-in button should be —
        # and there is nothing on the page to say the frame was refused.
        "frame-src https://oauth.telegram.org; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class WebServer(AccountRoutes, SignInRoutes, ConsoleRoutes, DocumentRoutes, LinkRoutes):
    def __init__(
        self,
        manager: PluginManager,
        database: Database,
        host: str,
        port: int,
        notifier=None,
    ):
        self._manager = manager
        self._notifier = notifier
        # Sends still in flight. The loop keeps only a weak reference to a
        # task, so one nobody holds can be collected before it finishes.
        self._sending: set[asyncio.Task] = set()
        self._database = database
        self._host = host
        self._port = port
        self._users = UsersRepository(database)
        self._sessions = SessionsRepository(database)
        self._links = TelegramLinksRepository(database)
        self._rate = RateLimiter()
        self._csrf = CsrfSigner(Config.CSRF_SECRET)
        self._runner: web.AppRunner | None = None
        self._settings = SettingsRepository(database)
        # The bot shares this manager, so a plugin switched off or maintenance
        # turned on here reaches the chat as well.
        manager.switches = self._settings
        self._demo = DemoService(manager, database, settings=self._settings)
        self._visits = VisitsRepository(database)
        self._feedback_log = FeedbackRepository(database)
        self._keys = ApiKeysRepository(database)
        self._push = PushRepository(database)
        self._live = LiveHub(PersonResolver(self._users, self._links))
        notify_all(self._live, manager.get_plugins())

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

    def _nav(self, request: web.Request, lang: str = i18n.DEFAULT) -> str:
        """The navigation for whoever is asking.

        Built per request rather than once at startup because it is not the
        same for everyone: a guest is offered the demonstration's copies of
        the plugin pages, since the real ones answer them with a login form —
        and only the ones that opted into the demonstration, since the rest
        have no public copy to point at. An administrator is offered the link
        to the area that is otherwise closed to them, which is a courtesy and
        not the reason it is closed.
        """
        user = resolve_user(request, self._users, self._sessions)
        is_guest = user is None

        menu = nav(
            request.path,
            self._manager.demo_pages() if is_guest else self._manager.pages(),
            guest=is_guest,
            admin=user.is_admin if user is not None else False,
            lang=lang,
        )
        base = i18n.split(request.path)[1]
        languages = i18n.LANGUAGES if user and user.is_admin else i18n.RELEASED
        switch = (
            language_switch(base, lang, languages) if base in DOCUMENT_PATHS else ""
        )

        if is_guest or is_public_demo_path(request.path):
            if not switch:
                return menu
            return attic(menu, f'<div class="account">{switch}</div>')

        token = self._csrf.session_token(
            request.cookies.get(SESSION_COOKIE, "")
        )

        return attic(menu, account_block(user.username, token, extra=switch, lang=lang))

    def _is_admin(self, request: web.Request) -> bool:
        user = resolve_user(request, self._users, self._sessions)
        return user is not None and user.is_admin

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
    async def _record_visit(self, request: web.Request, handler):
        response = await handler(request)

        if _is_a_visit(request, response) and not leaves_no_trace(request):
            self._visits.record(
                VIEW,
                path=request.path,
                identity=_visitor(request, self._users, self._sessions),
                referrer=_referrer_origin(request),
                **self._details(request),
            )

        return response

    def _details(self, request: web.Request) -> dict:
        """What the log may say about this browser: see `visitor.py`.

        A salt that cannot be read leaves the row without a visitor hash
        rather than the page without a response.
        """
        agent = request.headers.get("User-Agent", "")

        try:
            salt = self._visits.daily_salt()
        except Exception:
            logger.exception("Visitor salt unavailable")
            hashed = None
        else:
            hashed = visit_details.fingerprint(salt, client_ip(request), agent)

        return {
            "visitor": hashed,
            "agent": visit_details.describe(agent),
            "bot": visit_details.is_bot(agent),
            "campaign": visit_details.campaign(request),
        }

    @web.middleware
    async def _nonce(self, request: web.Request, handler):
        """Add the theme, then write the script nonce into body and header.

        Outermost on purpose. A nonce that governs the response has to be there
        for everything the inner layers can return, including the refusals they
        answer with instead of calling the handler.

        Minted per response rather than per process: a nonce that outlived one
        response would be readable by whatever injected the script it guards,
        which is the whole thing a nonce is for.
        """
        response = await handler(request)

        if response.content_type == "text/html":
            value = secrets.token_urlsafe(16)
            # The theme goes in here because this is the one place every page
            # passes through, the login form and the 404 included. A page left
            # out would flash dark at a reader of the light theme.
            body = response.text

            if "</head>" in body:
                body = body.replace("</head>", theme_head(body) + "</head>", 1)

            response.text = body.replace(CSP_NONCE_PLACEHOLDER, value)

            response.headers.setdefault(
                "Content-Security-Policy", _content_security_policy(value)
            )

        return response

    @web.middleware
    async def _maintenance(self, request: web.Request, handler):
        """While maintenance is on, everybody but an administrator gets a
        holding page.

        A 503 with `Retry-After`, which a search engine reads as "come back"
        rather than "gone". The sign-in form, the admin area and the assets
        they need stay open, or nobody could switch the mode off again.
        """
        if not self._settings.maintenance() or _open_in_maintenance(request.path):
            return await handler(request)

        user = self._current_user(request)

        if user is not None and user.is_admin:
            return await handler(request)

        headers = {"Retry-After": str(MAINTENANCE_RETRY_SECONDS)}

        if request.path.startswith(("/api/", "/demo/api/")) or request.method != "GET":
            return web.json_response(
                {"error": "Сервис на обслуживании. Попробуйте чуть позже."},
                status=503,
                headers=headers,
            )

        return web.Response(
            text=_render_maintenance(),
            content_type="text/html",
            status=503,
            headers=headers,
        )

    @web.middleware
    async def _authenticate(self, request: web.Request, handler):
        denied = verify_access(request)

        if denied is not None:
            return denied

        if (
            request.path in PUBLIC_PATHS
            or request.path.startswith("/fonts/")
            or is_public_demo_path(request.path)
        ):
            return await handler(request)

        if isinstance(request.match_info.http_exception, web.HTTPNotFound):
            return web.Response(
                text=render_not_found(), content_type="text/html", status=404
            )

        if self._identity(request) is None:
            return self._login_required(request)

        return await handler(request)

    def build_app(self) -> web.Application:
        app = web.Application(
            middlewares=[
                self._nonce,
                self._maintenance,
                self._authenticate,
                self._record_visit,
            ]
        )
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
                *(web.get(path, self._document) for path in LOCALIZED_DOCUMENTS),
                web.post("/logout", self._logout),
                web.get("/account/telegram/link", self._telegram_link),
                web.post("/account/telegram/unlink", self._telegram_unlink),
                web.get("/", self._index),
                web.get("/api/commands", self._commands),
                web.post("/api/command", self._execute),
                web.post("/api/page/action", self._plugin_page_action),
                web.get("/api/events", self._events),
                web.get("/robots.txt", self._robots),
                web.get("/sitemap.xml", self._sitemap),
                web.get("/health", self._health),
                web.get("/favicon.ico", self._favicon),
                web.get("/favicon.svg", self._favicon),
                web.get("/og-image.png", self._og_image),
                *(web.get(path, self._app_asset) for path in APP_ASSETS),
                web.get("/about-hero.js", self._script),
                web.get("/about-page.js", self._script),
                web.get("/project-page.js", self._script),
                web.get("/project/trace", self._trace),
                web.get("/feedback.js", self._script),
                web.post("/feedback", self._feedback),
                web.post(LINKS_API_PATH, self._api_links),
                web.get(LINKS_SAVE_PATH, self._save_link_form),
                web.post(LINKS_SAVE_PATH, self._save_link_submit),
                web.get("/push.js", self._script),
                web.get("/sw.js", self._script),
                web.post("/api/push/subscribe", self._push_subscribe),
                web.post("/api/push/unsubscribe", self._push_unsubscribe),
                web.post("/api/push/test", self._push_test),
                web.post("/account/push/remove", self._push_remove),
                web.post("/account/quiet", self._quiet_hours),
                web.get("/fonts/{name}", self._fonts),
            ],
        )

        # A page route is registered per plugin that declared one, so the
        # platform owns the path table and a plugin never adds a route to the
        # web layer by hand. The handler resolves the plugin from the path, so
        # it works the same whichever route reached it.
        for plugin in self._manager.get_plugins():
            if plugin.page:
                app.router.add_get(plugin.page, self._plugin_page)

        # The demonstration is the only part of this application reachable
        # without a session, and it installs itself so that all of it is
        # readable in one place rather than spread through the route table.
        self._demo.install(app)

        # The administrative area installs itself the same way, and it brings
        # its own gate with it: everything under /admin is behind one check
        # that lives with the routes rather than beside each of them.
        AdminArea(
            self._users, self._sessions, self._visits, self._database, self._nav,
            self._csrf,
            settings=self._settings,
            manager=self._manager,
            notifier=self._notifier,
        ).install(app)

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

        self._live.close()
        await self._runner.cleanup()
        self._runner = None

        logger.info("Web interface stopped")


async def _security_headers(request: web.Request, response: web.Response) -> None:
    """Attach the security headers to everything leaving the app.

    HSTS is added only when the Secure flag is on, which the deployment keeps
    on for every TLS endpoint: on a plain-http prefix the header would be a
    promise nothing honours.

    The content policy is not here but in `_nonce`, because a nonce is written
    into the body and a body cannot be rewritten at this point: the length is
    already settled, and changing it produces a 400.
    """
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)

    if Config.SESSION_COOKIE_SECURE:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000"
        )


def _is_a_visit(request: web.Request, response: web.StreamResponse) -> bool:
    """Whether this request is worth a row in the log.

    A successful read of a page the public can see, and nothing else. The
    console is left out on purpose: it is where the owner works, it would bury
    everything else, and it is already known. Sign-in and sign-up are recorded
    by the handlers that succeed at them, because the moment they happen is
    not the moment the request arrives.

    Only a 2xx or a redirect. A page that answered 403 or 500 was not read by
    anybody, and a log of attempts to read pages is a different table.
    """
    if request.method != "GET":
        return False

    if not (200 <= response.status < 400):
        return False

    if request.path in RELEASED_DOCUMENTS:
        return True

    return is_public_demo_path(request.path)


def _referrer_origin(request: web.Request) -> str | None:
    """The origin that led here, from the Referer header, or None.

    Shortened to scheme + host, so a search engine's path and query never reach
    the log: the origin answers "which site led here" without collecting what
    somebody typed into it. An empty or unparseable header is no referrer, the
    same as a browser that withholds the header or a direct visit.
    """
    header = request.headers.get("Referer", "").strip()

    if not header:
        return None

    try:
        parts = urlsplit(header)
    except ValueError:
        return None

    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None

    return f"{parts.scheme}://{parts.netloc}"


def _visitor(
    request: web.Request,
    users: UsersRepository,
    sessions: SessionsRepository,
) -> str | None:
    """Who the log should name, or None when there is nobody to name.

    A demonstration guest leaves its identity on the request, because the
    handler is what mints the token: a visitor arriving without a cookie has
    none on the request either, so reading it here would produce a second
    token and file the visit under a namespace that never existed.

    A reader with a session has an account. A reader with neither has
    neither, and inventing a name for them would be inventing a person.
    """
    identity = resolve_identity(request, users, sessions)

    if identity is not None:
        return str(identity)

    guest = request.get(VISITOR)

    return str(guest) if guest is not None else None


#: What a visitor is told to wait during maintenance.
MAINTENANCE_RETRY_SECONDS = 600


def _open_in_maintenance(path: str) -> bool:
    """The paths that keep working in maintenance: the way in, the admin area,
    the health check and the assets the pages need."""
    return (
        path in ("/login", "/logout", "/health", "/favicon.ico", "/favicon.svg")
        or path == "/admin"
        or path.startswith(("/admin/", "/fonts/"))
    )


def _render_maintenance() -> str:
    source = (TEMPLATES_DIR / "maintenance.html").read_text(encoding="utf-8")

    return source.replace(
        "__PAGE_STYLES__",
        (TEMPLATES_DIR / "_page_styles.html").read_text(encoding="utf-8"),
    )
