"""The pages that act on the platform rather than on one person's data.

Not a plugin, and deliberately so. A plugin is a unit of user functionality
with its own commands, its own tables and its own page; this is the opposite —
it reads across every plugin's data and belongs to the deployment, not to a
feature. Putting it in `app/plugins/` would have meant a plugin that touches
tables it does not own, and a `BasePlugin` flag whose only job is to mark a
page as privileged.

The gate is a middleware on a sub-application rather than a line in each
handler. That is the whole point of this module: a new route added to the
sub-application below is covered without anybody remembering, and a route
registered on the main application by mistake is simply not under `/admin` and
therefore not here. A check that has to be repeated per handler is a check that
eventually is not repeated.

What a non-administrator gets is a 404 and not a 403. A 403 says the address
exists and only the permission is missing, which is an answer a stranger does
not need. The pages are marked `noindex` as well, though robots.txt already
covers them: a page nobody should be able to reach should not be a search
result if it is ever reached.
"""

from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from app.db.sessions_repo import SessionsRepository
from app.db.users_repo import UsersRepository
from app.web.auth import resolve_user
from app.web.pages import menu_styles

ADMIN_PREFIX = "/admin"

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

USERS: web.AppKey[UsersRepository] = web.AppKey("users", UsersRepository)
SESSIONS: web.AppKey[SessionsRepository] = web.AppKey(
    "sessions", SessionsRepository
)

#: The body a refused request gets: an ordinary missing page, with `noindex`
#: on it for the case where a crawler ever arrives. The status is what has to
#: match the application's own answer for an address that does not exist, and
#: it does; the body is not claimed to be identical, because there is no
#: missing-page template in this application to be identical to.
NOT_FOUND_BODY = (
    "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
    '<meta name="robots" content="noindex, nofollow">'
    "<title>Страница не найдена</title>"
    "</head><body><h1>Страница не найдена</h1></body></html>"
)


@web.middleware
async def require_admin(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    """Refuse everything that is not an administrator, before the handler.

    Both refusals are what this application already says to somebody in the
    same position, which is the point of copying rather than inventing: a
    visitor with no session gets the same redirect to the sign-in form as
    every other address, and a visitor with a session gets the same 404 as
    every other address that is not there. Neither answer says whether this
    one is real, and neither is a new thing to look different.

    The flag comes from the database on every request rather than from the
    session, so `revoke-admin` closes the door immediately instead of at the
    next sign-in.

    Responses are returned rather than exceptions raised, as everywhere else
    in this web layer: returning an `HTTPException` from a handler is
    deprecated in aiohttp, and a middleware that short-circuits is a better
    place to stop anyway.
    """
    user = resolve_user(request, request.app[USERS], request.app[SESSIONS])

    if user is None:
        return web.Response(status=303, headers={"Location": "/login"})

    if not user.is_admin:
        return web.Response(
            status=404,
            text=NOT_FOUND_BODY,
            content_type="text/html",
            charset="utf-8",
        )

    return await handler(request)


class AdminArea:
    """The administrative sub-application."""

    def __init__(
        self,
        users: UsersRepository,
        sessions: SessionsRepository,
        nav_for: Callable[[web.Request], str],
    ) -> None:
        self._users = users
        self._sessions = sessions
        # The same navigation everybody else gets, handed in rather than
        # rebuilt. `nav()` exists so that there is one menu and not five
        # copies of it; an administrative page with a second implementation
        # is the same bug wearing a different hat.
        self._nav_for = nav_for

    def install(self, app: web.Application) -> None:
        """Put the area on an application, behind the one gate.

        Registered from here rather than from the main route table so the
        whole of it is readable in one place. The routes exist only inside a
        sub-application that has the middleware attached, which is what makes
        "a route without a check" impossible rather than merely unlikely.
        """
        sub = web.Application(middlewares=[require_admin])
        sub[USERS] = self._users
        sub[SESSIONS] = self._sessions
        sub.router.add_get("", self.landing)

        app.add_subapp(ADMIN_PREFIX, sub)

    async def landing(self, request: web.Request) -> web.Response:
        """The empty administrative area.

        Nothing lives here yet. The page exists because a permission with no
        page is an untested permission: the gate above it would be code that
        no test can reach until something is behind it.
        """
        source = (TEMPLATES_DIR / "admin.html").read_text(encoding="utf-8")
        source = source.replace("__NAV__", self._nav_for(request))
        source = source.replace("__MENU_STYLES__", menu_styles())
        source = source.replace(
            "__PAGE_STYLES__",
            (TEMPLATES_DIR / "_page_styles.html").read_text(encoding="utf-8"),
        )

        return web.Response(text=source, content_type="text/html", charset="utf-8")
