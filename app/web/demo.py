"""The public demonstration.

A visitor who arrives at a resume has no account and is not going to make one
to look at a project. So the demonstration is reachable without a session,
under its own paths, against its own namespace, and it is capped.

Three rules hold this together, and each of them exists because breaking it
would not be a bug but a hole:

- **A guest never resolves a session.** The handlers here build the
  identity from a cookie and from nothing else. If one of them ever reached
  for ``resolve_identity``, a visitor would be able to run commands as a real
  account — the demonstration is the one place on the site that is reachable
  without signing in, so it is the one place that must not be able to act as
  somebody. That is why the identity is threaded through as an argument and
  never looked up.
- **A guest is not an account.** There is no row in ``users``, no password
  and no session, so there is nothing to authenticate and nothing to leak.
  What it writes lives under ``web:guest:<token>``, which is what makes
  cleaning up a delete by prefix rather than a walk over accounts.
- **A guest is capped.** The demonstration is on the open internet and its
  namespace is the only one an anonymous visitor can write to, so the plugins
  refuse a write past a limit.

The namespaces are filled on arrival rather than on a schedule: a guest with
nothing in them is given the seed, and one whose data was cleaned up is given
it again. That is why the cleaner can run at any time without coordinating
with anything — a visitor never sees an empty demonstration, because arriving
is what fills it.
"""

import logging
import secrets
from collections.abc import Mapping

from aiohttp import web

from app.core.config import Config
from app.core.identity import Identity, guest
from app.core.results import CommandError

from .pages import nav, render_demo_landing, render_plugin_page
from .ratelimit import RateLimiter, client_ip

logger = logging.getLogger(__name__)

# The guest's half of the cookie. HttpOnly because nothing on the page needs
# to read it, and SameSite=Lax so it rides the top-level navigation back from
# a vacancy site and nothing else.
COOKIE = "platform_demo"
COOKIE_TTL_SECONDS = 7 * 86400

#: Where a handler leaves the identity it used, for whatever runs after it.
#: The guest token is minted here and set as a cookie in the response; a
#: visitor arriving with none has no cookie on the *request* yet, so anything
#: reading the request afterwards would mint a second, different token and
#: record a namespace that never existed.
VISITOR = "demo_visitor"
TOKEN_BYTES = 16

DEMO_PREFIX = "/demo"


def read_guest(request: web.Request) -> tuple[Identity, str]:
    """The guest this browser is, minting one if it has none.

    Returns the identity and the token, because the caller has to set the
    cookie and the token is the only record that it was issued. An existing
    token is reused: a new one on every request would give every visitor a
    fresh namespace on every click, so the demonstration would reset itself
    under the reader's hands.
    """
    token = request.cookies.get(COOKIE, "")

    if not _usable(token):
        token = secrets.token_urlsafe(TOKEN_BYTES)

    return guest(token), token


def _usable(token: str) -> bool:
    """Whether a cookie carries a token this module is willing to keep.

    A cookie is caller-controlled, so it is checked rather than trusted. The
    length is the floor of what this module issues, which is enough to keep a
    forged one from naming a namespace that another visitor's token could.
    """
    return len(token) >= 16 and all(
        character.isalnum() or character in "-_" for character in token
    )


def set_guest_cookie(response: web.Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=COOKIE_TTL_SECONDS,
        path=DEMO_PREFIX,
        httponly=True,
        samesite="lax",
        # Not Secure, for the same reason the CSRF cookie is not: a
        # demonstration reached over plain http on a corporate network should
        # still work, and the token names a namespace of made-up data.
        secure=False,
    )


def is_public_demo_path(path: str) -> bool:
    """Whether a path belongs to the demonstration and needs no session.

    Matched on the prefix and the slash, so `/demo` and `/demo/...` are public
    and nothing else is. A bare `startswith("/demo")` would also have opened
    `/demoted` — and a path that looked like the demonstration but was not
    would be a route the access rules and the demonstration disagree about.
    """
    return path == DEMO_PREFIX or path.startswith(DEMO_PREFIX + "/")


class DemoService:
    """Serves the demonstration: the landing page, the plugin pages, the
    actions, and the cleanup that keeps the namespaces from accumulating."""

    def __init__(self, manager, database):
        self._manager = manager
        self._database = database
        self._rate = RateLimiter()

    # -- the limit ----------------------------------------------------------

    def _limited(self, request: web.Request) -> web.Response | None:
        """Refuse the request when the address has had its allowance.

        Applied to the pages and not only to the actions, because the pages
        are where the writing happens: a namespace is seeded on arrival, so a
        plain page load with a fresh cookie costs about two dozen rows. Rate
        limiting the submits alone would have left the cheaper path open.

        Keyed on the address rather than on the guest, because the guest is a
        caller-controlled cookie and a new one costs nothing to mint — which
        is exactly what the caps inside the plugins do not stop.
        """
        allowed = self._rate.allow(
            f"demo|{client_ip(request)}",
            Config.DEMO_ATTEMPTS_PER_WINDOW,
            Config.DEMO_WINDOW_SECONDS,
        )

        if allowed:
            return None

        logger.warning(
            "Demonstration rate limit reached from %s", client_ip(request)
        )

        if request.path.startswith("/demo/api/"):
            return web.json_response(
                {"error": "Слишком много запросов. Попробуйте позже."},
                status=429,
            )

        return web.Response(
            text=(
                "<p>Слишком много запросов с этого адреса. "
                "Попробуйте через час.</p>"
            ),
            content_type="text/html",
            status=429,
        )

    # -- pages --------------------------------------------------------------

    async def landing(self, request: web.Request) -> web.Response:
        """The front door: what this is, and the things to look at."""
        limited = self._limited(request)

        if limited is not None:
            return limited

        links = self._manager.demo_pages()

        return web.Response(
            text=render_demo_landing(links, self._nav()),
            content_type="text/html",
        )

    async def page(self, request: web.Request) -> web.Response:
        """A plugin's page, rendered for a guest."""
        limited = self._limited(request)

        if limited is not None:
            return limited

        # The route captures the segment without its leading slash, while a
        # plugin declares "/expenses". It goes through `find_by_page`, so a
        # segment that names no plugin simply has no owner and 404s — the
        # visitor-supplied part never reaches anything but a lookup.
        captured = request.match_info.get("page", "")
        plugin = self._manager.find_by_page(f"/{captured}")

        if plugin is None or not plugin.demo:
            # The check is here and not only in the navigation, because a link
            # being absent is not the same as the page being unreachable: the
            # address is a guess away. A plugin that did not ask to be
            # published gets a 404 rather than a page, and says nothing about
            # whether it exists.
            return web.Response(status=404)

        page_path = plugin.page
        identity, token = read_guest(request)
        request[VISITOR] = identity

        view = await self._view(plugin, identity)

        body = render_plugin_page(page_path, view, self._nav(), demo=True)

        response = web.Response(text=body, content_type="text/html")
        set_guest_cookie(response, token)

        return response

    async def action(self, request: web.Request) -> web.Response:
        """Something was done on a guest page; answer with it redrawn."""
        limited = self._limited(request)

        if limited is not None:
            return limited

        payload = await self._payload(request)

        if payload is None:
            return web.json_response(
                {"error": "Request body must be a JSON object"},
                status=400,
            )

        plugin = self._manager.find_by_page(payload.get("page", ""))

        if plugin is None:
            return web.json_response({"error": "Unknown page"}, status=404)

        # Read from the cookie and nowhere else. See the module docstring:
        # this route is reachable without a session, so what it resolves to
        # has to be decided by the cookie alone.
        identity, token = read_guest(request)
        request[VISITOR] = identity

        try:
            view = await plugin.page_action(
                identity,
                payload.get("action", ""),
                payload.get("payload", {}),
            )
        except CommandError as error:
            return web.json_response({"error": str(error)}, status=400)
        except Exception:
            logger.exception(
                "Demo action %s failed on %s",
                payload.get("action", ""),
                plugin.name,
            )
            return web.json_response({"error": "Internal error"}, status=500)

        response = web.json_response({"view": view})
        set_guest_cookie(response, token)

        return response

    async def _view(self, plugin, identity: Identity) -> dict:
        """The plugin's description for a guest, filling an empty namespace.

        The fill happens on arrival rather than on a timer, which is what lets
        the cleanup run whenever it likes: a guest whose rows were removed
        finds the namespace empty again and is given the same demonstration a
        first-time visitor gets.
        """
        if await self._is_empty(plugin, identity):
            await self._seed(plugin, identity)

        return await plugin.page_view(identity)

    async def _is_empty(self, plugin, identity: Identity) -> bool:
        """Whether this plugin has nothing to show for this guest."""
        if hasattr(plugin.repo, "categories"):
            return not plugin.repo.categories(identity)

        return not plugin.repo.entries(identity)

    async def _seed(self, plugin, identity: Identity) -> None:
        try:
            await plugin.seed_demo(identity)
        except Exception:
            # A demonstration with nothing in it is a poor demonstration, not a
            # broken page. Logged and carried on: the visitor can still enter
            # their own data, which is the part they came to judge.
            logger.exception("Demo seed failed for %s", plugin.name)

    async def _payload(self, request: web.Request) -> Mapping | None:
        try:
            body = await request.json()
        except Exception:
            return None

        if not isinstance(body, dict):
            return None

        if not isinstance(body.get("page", ""), str):
            return None

        if not isinstance(body.get("action", ""), str):
            return None

        if not isinstance(body.get("payload", {}), dict):
            return None

        return body

    def _nav(self) -> str:
        """The guest's navigation, always the guest's.

        Takes no request and resolves nothing: what a guest may be offered is
        a fixed answer, decided by the prefix and not by whoever happens to
        be holding the cookie.
        """
        return nav("", self._manager.demo_pages(), guest=True)

    # -- routes -------------------------------------------------------------

    def install(self, app: web.Application) -> None:
        """Put the demonstration on an application.

        Registered from here rather than from the main route table so that the
        whole of it is visible in one place — this is the part of the web
        layer that is reachable without a session, and that should be
        something a reader can check rather than something they have to find.
        """
        app.router.add_get(DEMO_PREFIX, self.landing)
        app.router.add_get(DEMO_PREFIX + "/{page}", self.page)
        app.router.add_post(DEMO_PREFIX + "/api/action", self.action)

    # -- cleanup ------------------------------------------------------------

    def prune(self) -> dict[str, int]:
        """Delete every guest namespace, reporting what went.

        A delete by prefix rather than a walk over accounts, because a guest
        was never in `users`. Every table a demonstration can write to is
        named here; a table added later and not added here is one whose guests
        are never cleaned up, so the list is short and explicit on purpose
        rather than discovered from the schema.
        """
        pattern = "web:guest:%"
        removed = {}

        with self._database.connect() as conn:
            with conn.cursor() as cur:
                for table in (
                    "expenses",
                    "expense_categories",
                    "job_applications",
                ):
                    cur.execute(
                        f"DELETE FROM {table} WHERE user_id LIKE %s",
                        (pattern,),
                    )
                    removed[table] = cur.rowcount
            conn.commit()

        return removed
