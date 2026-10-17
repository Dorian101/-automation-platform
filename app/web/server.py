import hmac
import html
import logging
import os
import re
import secrets
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from aiohttp import web
from psycopg.errors import UniqueViolation

from app.core.config import Config
from app.core.identity import Identity
from app.core.results import CommandError
from app.db.database import Database
from app.db.links_repo import TelegramLinksRepository
from app.db.migrations import MigrationRunner
from app.db.sessions_repo import SessionsRepository
from app.db.users_repo import UsersRepository
from app.db.visits_repo import SIGNIN, SIGNUP, VIEW, VisitsRepository
from app.plugins.manager import PluginManager

from .admin import AdminArea
from .auth import (
    SESSION_COOKIE,
    clear_session_cookie,
    resolve_identity,
    resolve_user,
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
    is_probably_a_token,
    new_token,
)
from .demo import VISITOR, DemoService, is_public_demo_path
from .pages import account_block, attic, menu_styles, nav, render_plugin_page
from .ratelimit import RateLimiter, client_ip
from .responses import redirect as _redirect
from .responses import token_rejected
from .telegram_auth import TelegramAuthError, verify_login

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# The tab icon, beside this module rather than in `templates/`: it is an asset,
# not a page, and nothing renders it as markup.
FAVICON_SVG = Path(__file__).resolve().parent / "favicon.svg"

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
#
# /robots.txt and /sitemap.xml are fetched by a crawler that has no session and
# cannot have one. Left out of this set they answered a redirect to the login
# form, which a search engine reads as "nothing to see here" — the pages it
# was meant to publish were not reachable from anywhere on the site, so it had
# no way to find them.
PUBLIC_PATHS = {
    "/login",
    "/signup",
    "/about",
    "/project",
    "/health",
    "/robots.txt",
    "/sitemap.xml",
    "/favicon.ico",
    "/favicon.svg",
}

# What a request carries to say it is the owner's own working rather than a
# visit. Named beside the proxy's X-Platform-Auth because it is the same kind
# of thing — something only the holder of the value knows — and different in
# what it is for: the auth header is the door, this one is the log.
PROBE_HEADER = "X-Platform-Probe"

# The public address of the site, for the sitemap. There is no request to read
# a host from: the app sits behind a reverse proxy, so the Host header is
# whatever the proxy says and cannot be trusted to be the public name. One
# setting beats guessing from an untrusted header.
#
# Empty by default, and the default is the point. A default here would be one
# deployment's domain written into every clone of this repository, which is
# both somebody else's infrastructure and a sitemap that names a host this
# installation has never heard of. Unset, there is no sitemap — see
# `_sitemap`. Set it in `.env` to the name your readers type.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

#: What the site calls itself, for a link shared in a chat. Without it the
#: messenger puts the bare address in the card, which is the one line of the
#: preview nobody reads.
#:
#: The same words are written out in the card and console templates. They are
#: not read from here, and that is on purpose: pulling a heading out of a
#: template to put it in a meta tag makes the two dependent, and the one that
#: would break is the one nobody sees on the page.
SITE_NAME = "Платформа автоматизации"

# What a search engine is allowed to index. Everything else on this site either
# needs a session or is the console itself, and there is nothing in the
# console a reader would want in an index anyway.
INDEXABLE_PATHS = ("/about", "/project", "/demo")

# Sent on every response, so no page or JSON answer can ever forget one. CSP
# allows the Telegram Login Widget's script from telegram.org and inline
# scripts and styles, which the console relies on; everything else stays
# locked to the app's own origin. form-action 'self' is what keeps a stray
# <form> from shipping a session cookie somewhere it does not belong.
#: Where a template asks for the nonce. Substituted into the body and into the
#: header by one place, so a template never has to be handed the value to use
#: it — which is what keeps this to one edit rather than one per render path.
CSP_NONCE_PLACEHOLDER = "__CSP_NONCE__"


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
        self._csrf = CsrfSigner(Config.CSRF_SECRET)
        self._runner: web.AppRunner | None = None
        self._demo = DemoService(manager, database)
        self._visits = VisitsRepository(database)

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

        return _render_plugin_page(plugin.page, view, self._nav(request))

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
            _form_value(form, CSRF_FIELD),
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
        return _render_page("about.html", self._nav(request), path=request.path)

    async def _project(self, request: web.Request) -> web.Response:
        return _render_page(
            "project.html",
            self._nav(request),
            replacements=self._project_counts(),
            path=request.path,
        )

    def _project_counts(self) -> dict[str, str]:
        """The three numbers the project page states about itself.

        Read from the plugin manager rather than written into the template,
        because a number typed into a page by hand is a number that starts
        being wrong the moment anything is added. These three cannot drift:
        the page and the tests are both looking at the same source.
        """
        plugins = self._manager.get_plugins()

        return {
            "__PLUGINS__": str(len(plugins)),
            "__COMMANDS__": str(sum(len(p.commands) for p in plugins)),
            "__MIGRATIONS__": str(
                len(MigrationRunner(self._database).discover_migrations())
            ),
        }

    async def _commands(self, request: web.Request) -> web.Response:
        """The commands worth typing.

        A plugin that owns a page is left out. Its commands exist only so the
        router can refuse them with "web console only" rather than "unknown
        command", and listing them here would offer somebody something that
        cannot work: the page is in the menu, and the two say the same thing
        twice.

        The filter is here rather than in the page because the answer is a
        fact about the plugin, and a page that hid what it was given would
        need to know the rule to hide it.
        """
        plugins = [
            {
                "name": plugin.name,
                "version": plugin.version,
                "description": plugin.description,
                "commands": plugin.commands,
            }
            for plugin in self._manager.get_plugins()
            if not plugin.web_only and plugin.commands
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

    async def _robots(self, request: web.Request) -> web.Response:
        """Tell a crawler where it may look.

        A crawler arrives with no session and cannot get one, so this route
        has to exist and has to be public — served as text/plain, because a
        crawler reads the body and ignores the content type it would prefer.
        """
        lines = [
            "User-agent: *",
            "Allow: " + "\nAllow: ".join(INDEXABLE_PATHS),
            # The sitemap, too. It is not page content and nothing here wants it
            # indexed, but Google refuses to fetch a sitemap that robots.txt
            # disallows — which made the file unreachable and left the Search
            # Console reporting a fetch failure against a perfectly good 200.
            "Allow: /sitemap.xml",
            # Everything else is either behind a session or the console. Saying
            # so is clearer than leaving a crawler to work through a login form
            # and conclude the site is empty.
            "Disallow: /",
        ]

        return web.Response(
            text="\n".join(lines) + "\n",
            content_type="text/plain",
        )

    async def _sitemap(self, request: web.Request) -> web.Response:
        """The public pages, listed.

        Written by hand rather than by library: there are three of them, and
        the routes are already named in ``INDEXABLE_PATHS`` beside the robots
        file that points here. A generated sitemap would need the public host
        name from somewhere, and there is nowhere trustworthy to read it from
        behind the proxy — see ``PUBLIC_BASE_URL``.

        Absent, there is no sitemap and this is a 404. A file of relative URLs
        is not a sitemap: a search engine rejects it, and a console showing an
        error about a file nobody asked for is worse than one that is not
        there.
        """
        if not PUBLIC_BASE_URL:
            return web.Response(status=404)

        today = date.today().isoformat()

        entries = "".join(
            f"<url><loc>{PUBLIC_BASE_URL}{path}</loc>"
            f"<lastmod>{today}</lastmod></url>"
            for path in INDEXABLE_PATHS
        )

        return web.Response(
            text=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"{entries}</urlset>"
            ),
            content_type="application/xml",
        )

    async def _favicon(self, request: web.Request) -> web.Response:
        """The tab icon, at both names a browser might ask for.

        Every browser requests `/favicon.ico` on its own when a page names no
        icon, and answers 404 for the tab until one turns up. It used to answer
        303 towards the sign-in form instead, because this route did not exist
        and anything outside PUBLIC_PATHS is treated as somebody's page — so the
        icon was a login screen, and the request counted as a visit.

        Served from a file rather than assembled here, and left without a
        `<link rel="icon">` in the templates on purpose: the documents are built
        in thirteen places, and thirteen copies of one line to avoid a file
        request is the wrong trade. The convention does the same job with none.
        """
        return web.Response(
            body=FAVICON_SVG.read_bytes(),
            content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

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

        return self._render_login(request)

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
                client_ip(request),
            )
            return self._render_login(request,
                error=FORGED_FORM_MESSAGE,
                username=username,
                status=400,
            )

        # The address window is checked first and its key carries no username.
        # Order is the point: this is the window that stops a caller producing a
        # fresh key per name tried, so it has to run before the per-user window
        # that creates those keys.
        if not self._rate.allow(
            _rate_key(request, "login-address"),
            Config.LOGIN_ATTEMPTS_PER_ADDRESS_PER_WINDOW,
            Config.RATE_LIMIT_WINDOW_SECONDS,
        ):
            logger.warning(
                "Login rate limit reached for the address %s",
                client_ip(request),
            )
            return self._render_login(request,
                error="Слишком много попыток. Попробуйте позже.",
                username=username,
                status=429,
            )

        if not self._rate.allow(
            _rate_key(request, "login", username),
            Config.LOGIN_ATTEMPTS_PER_WINDOW,
            Config.RATE_LIMIT_WINDOW_SECONDS,
        ):
            logger.warning(
                "Login rate limit reached for %r from %s",
                username,
                client_ip(request),
            )
            return self._render_login(request,
                error="Слишком много попыток. Попробуйте позже.",
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
                "Failed login attempt for %r from %s", username, client_ip(request)
            )
            return self._render_login(request,
                error="Неверное имя пользователя или пароль.",
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

        logger.info("Signed in %r from %s", user.username, client_ip(request))

        if not _leaves_no_trace(request):
            self._visits.record(
                SIGNIN, path="/login", identity=str(self._users.identity_for(user))
            )

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
            return token_rejected(request, "/login")

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

        return self._render_signup(request)

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
                client_ip(request),
            )
            return self._render_signup(request,
                error=FORGED_FORM_MESSAGE,
                username=username,
                status=400,
            )

        if not self._rate.allow(
            _rate_key(request, "signup"),
            Config.SIGNUP_ATTEMPTS_PER_WINDOW,
            Config.SIGNUP_WINDOW_SECONDS,
        ):
            logger.warning("Sign-up rate limit reached from %s", client_ip(request))
            return self._render_signup(request,
                error="Слишком много попыток. Попробуйте позже.",
                username=username,
                status=429,
            )

        if not signup_allowed(invite):
            return self._render_signup(request,
                error="Неверный инвайт-код.",
                username=username,
                status=403,
            )

        try:
            user = self._users.create(username, password)
        except ValueError as error:
            # PasswordError is a ValueError, so a short password and an empty
            # username both arrive here carrying a message written for the
            # person filling in the form.
            return self._render_signup(request,
                error=str(error),
                username=username,
                status=400,
            )
        except UniqueViolation:
            return self._render_signup(request,
                error="Это имя уже занято.",
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
            "Account created for %r from %s", user.username, client_ip(request)
        )

        if not _leaves_no_trace(request):
            self._visits.record(
                SIGNUP, path="/signup", identity=str(self._users.identity_for(user))
            )

        return response

    def _render_login(
        self,
        request: web.Request,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        token = _form_token(request)

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
        request: web.Request,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        token = _form_token(request)

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

    def _nav(self, request: web.Request) -> str:
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
        )

        if is_guest or is_public_demo_path(request.path):
            return menu

        token = self._csrf.session_token(
            request.cookies.get(SESSION_COOKIE, "")
        )

        return attic(menu, account_block(user.username, token))

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

        if _is_a_visit(request, response) and not _leaves_no_trace(request):
            self._visits.record(
                VIEW,
                path=request.path,
                identity=_visitor(request, self._users, self._sessions),
            )

        return response

    @web.middleware
    async def _nonce(self, request: web.Request, handler):
        """Write the script nonce into the body, then into the header.

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
            body = response.text

            if CSP_NONCE_PLACEHOLDER in body:
                response.text = body.replace(CSP_NONCE_PLACEHOLDER, value)

            response.headers.setdefault(
                "Content-Security-Policy", _content_security_policy(value)
            )

        return response

    @web.middleware
    async def _authenticate(self, request: web.Request, handler):
        denied = verify_access(request)

        if denied is not None:
            return denied

        if request.path in PUBLIC_PATHS or is_public_demo_path(request.path):
            return await handler(request)

        if self._identity(request) is None:
            return self._login_required(request)

        return await handler(request)

    def build_app(self) -> web.Application:
        app = web.Application(
            middlewares=[self._nonce, self._authenticate, self._record_visit]
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
                web.get("/about", self._about),
                web.get("/project", self._project),
                web.post("/logout", self._logout),
                web.get("/account/telegram/link", self._telegram_link),
                web.post("/account/telegram/unlink", self._telegram_unlink),
                web.get("/", self._index),
                web.get("/api/commands", self._commands),
                web.post("/api/command", self._execute),
                web.post("/api/page/action", self._plugin_page_action),
                web.get("/robots.txt", self._robots),
                web.get("/sitemap.xml", self._sitemap),
                web.get("/health", self._health),
                web.get("/favicon.ico", self._favicon),
                web.get("/favicon.svg", self._favicon),
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
            self._users, self._sessions, self._visits, self._nav, self._csrf
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


def _rate_key(request: web.Request, action: str, name: str = "") -> str:
    """Key an attempt, binding it to the address that made it.

    For login the username rides along too, so one person's typos do not lock
    out a whole network behind the proxy. Sign-ups are keyed by address alone:
    anyone registering is expected to hand out an invite code first.
    """
    return f"{action}|{client_ip(request)}|{name.strip().lower()}"


def _render_index(
    link,
    nonce: str,
    nav_html: str,
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

    source = source.replace("__NAV__", nav_html)
    source = source.replace("__MENU_STYLES__", menu_styles())
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

    if request.path in ("/about", "/project"):
        return True

    return is_public_demo_path(request.path)


def _leaves_no_trace(request: web.Request) -> bool:
    """Whether this request belongs to nobody the log is counting.

    The owner checks the site more than a stranger visits it, and every check
    lands in the same log. There is nothing on a row to recognise them by — no
    address is stored — so the comparison happens here, on the way in, and the
    address is not kept. Filtering on the way out instead would leave the
    counts and the growth rate counting the owner, which is what the page is
    read for.

    The address is the one the reverse proxy wrote, and Caddy replaces
    `X-Forwarded-For` rather than passing a caller's through — checked against
    the running deployment, not assumed: a spoofed header came back showing the
    real address. Anything that made the proxy trust an incoming value would
    reopen this, and would only cost a reader their own rows back.

    An address covers only the machine it was written on. The secret covers
    requests from anywhere, which is what measuring needs: those come from a
    different address every time and none of them is in the list. Both are
    compared on the way in and neither is written down.
    """
    if Config.VISIT_PROBE_TOKEN and hmac.compare_digest(
        request.headers.get(PROBE_HEADER, "").strip(),
        Config.VISIT_PROBE_TOKEN,
    ):
        return True

    if not Config.VISIT_IGNORE_IPS:
        return False

    return client_ip(request) in Config.VISIT_IGNORE_IPS


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


def _form_token(request: web.Request) -> str:
    """The token to print into a form and to hand the browser as a cookie.

    Reuses whatever the browser already holds rather than minting a new one.
    Minting on every page load meant that two open tabs disagreed: the first
    tab was given a cookie and a form field, the second tab overwrote the
    cookie, and the browser then sent the *new* cookie with the *old* form.
    Double submission is meant to be the pair that matches, so any page load
    that changes one half without the other breaks it — and it broke in a way
    that looked like the site was refusing to let the owner in.

    A value that fails `is_probably_a_token` is replaced, so a cookie that
    was mangled in transit does not become the form's token as well.
    """
    existing = request.cookies.get(CSRF_COOKIE, "")

    if is_probably_a_token(existing):
        return existing

    return new_token()


def _set_csrf_cookie(response: web.Response, token: str) -> web.Response:
    """Tie the double-submit token to the browser that was handed the form.

    HttpOnly and SameSite=Lax, like the session cookie itself: the value is
    not for script to read, the page supplies it to echo back, and same-site
    is enough because the whole point is that a *cross*-site form does not get
    to carry cookies.

    Secure follows the session cookie rather than being decided here, and the
    flag is the right one to follow: this cookie only matters to a form that
    ends in a session, so a deployment with `SESSION_COOKIE_SECURE` on is
    https-only by definition — a browser will not accept a Secure cookie over
    http, which means the session could not be established there whatever this
    cookie did.

    That is also why the demonstration's guest cookie stays insecure, which is
    the one thing this change does not do: that cookie carries a session with
    it nowhere, being the only part of the site that needs no account, so
    marking it Secure would break the demonstration over http and buy nothing.
    """
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=CSRF_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=Config.SESSION_COOKIE_SECURE,
    )

    return response


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


def _render_page(
    template: str,
    nav_html: str,
    status: int = 200,
    replacements: Mapping[str, str] | None = None,
    path: str = "",
) -> web.Response:
    """Render one of the public document pages.

    A page that has to work without a session must not quietly grow a
    dependency on one, so the substitutions here are the stylesheet and, where
    a page states a fact about the project, numbers read from the code. If a
    page starts needing anything per-visitor, that is the signal to reconsider
    whether it belongs in PUBLIC_PATHS at all.
    """
    source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    source = source.replace("__NAV__", nav_html)
    source = source.replace("__MENU_STYLES__", menu_styles())
    source = source.replace(
        "__PAGE_STYLES__",
        (TEMPLATES_DIR / "_page_styles.html").read_text(encoding="utf-8"),
    )

    for name, value in (replacements or {}).items():
        source = source.replace(name, value)

    if path:
        source = source.replace("</head>", _share_tags(source, path) + "</head>")

    return web.Response(text=source, content_type="text/html", status=status)


_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_DESCRIPTION = re.compile(r'name="description" content="(.*?)"', re.DOTALL)


def _share_tags(source: str, path: str) -> str:
    """What a messenger shows when the link is pasted.

    A résumé link is almost always passed on in a chat, and without these it
    arrives as a bare address: no title, no line of explanation, nothing that
    says what the page is before the reader decides to spend a second on it.

    Built from the page's own `<title>` and description rather than written
    beside them. A second copy of either is a second thing to keep right, and
    the one that goes wrong is the one nobody sees — it is not on the page.

    No `og:image`: there is no image to point at, and a tag naming a file
    that does not exist is worse than no tag. The base URL is only there
    because an absolute address is required, and a deployment without
    `PUBLIC_BASE_URL` set gets no url at all rather than a wrong one.
    """
    title = _TITLE.search(source)
    description = _DESCRIPTION.search(source)

    if title is None or description is None:
        return ""

    tags = [
        'property="og:type" content="website"',
        f'property="og:site_name" content="{SITE_NAME}"',
        f'property="og:title" content="{title.group(1)}"',
        f'property="og:description" content="{description.group(1)}"',
        'name="twitter:card" content="summary"',
    ]

    if PUBLIC_BASE_URL:
        tags.append(f'property="og:url" content="{PUBLIC_BASE_URL}{path}"')

    return "".join(f"<meta {tag}>\n" for tag in tags)


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

    return '<p class="alt">Нет аккаунта? <a href="/signup">Регистрация</a></p>'


def _render_plugin_page(page_path: str, view: dict, nav_html: str) -> web.Response:
    """Build a plugin's page.

    The painting lives in :mod:`app.web.pages`, which the demonstration uses
    as well: the same plugins, the same renderer, a different identity in
    front of them.
    """
    return web.Response(
        text=render_plugin_page(page_path, view, nav_html),
        content_type="text/html",
    )


def _form_value(form: Mapping[str, object], name: str) -> str:
    """Read a text field, rejecting anything that is not a plain string.

    A file upload arrives as a FileField, and str() on it would quietly turn
    the request into a bogus login attempt instead of a clean rejection.
    """
    value = form.get(name, "")
    return value if isinstance(value, str) else ""
