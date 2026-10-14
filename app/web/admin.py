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

import html
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from aiohttp import web

from app.db.sessions_repo import SessionsRepository
from app.db.users_repo import User, UsersRepository
from app.web.auth import SESSION_COOKIE, resolve_user
from app.web.csrf import CSRF_FIELD, CsrfSigner
from app.web.pages import menu_styles
from app.web.responses import form_forged, redirect

logger = logging.getLogger(__name__)

ADMIN_PREFIX = "/admin"
USERS_PATH = ADMIN_PREFIX + "/users"

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
        self._csrf = CsrfSigner()
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

        The id in the two action routes is matched against digits in the route
        itself, so a hand-made request with a name where a number belongs
        never reaches a handler and never reaches a query.
        """
        sub = web.Application(middlewares=[require_admin])
        sub[USERS] = self._users
        sub[SESSIONS] = self._sessions

        sub.router.add_get("", self.index)
        sub.router.add_get("/users", self.users_page)
        sub.router.add_post("/users/{user_id:\\d+}/disable", self.disable)
        sub.router.add_post("/users/{user_id:\\d+}/enable", self.enable)

        app.add_subapp(ADMIN_PREFIX, sub)

    async def index(self, request: web.Request) -> web.Response:
        """`/admin` leads to the section rather than describing the section.

        A landing page for an area with one thing in it is a page whose only
        content is a description of itself, and its reader does not need it.
        """
        return redirect(USERS_PATH)

    async def users_page(self, request: web.Request) -> web.Response:
        """The accounts, optionally narrowed to those matching `q`.

        A search is a GET: it changes nothing, and an address that can be
        bookmarked and sent to somebody is worth more here than a POST that
        cannot be.
        """
        query = request.query.get("q", "").strip()

        found = self._users.search(query)

        return self._page(found, query, request)

    async def disable(self, request: web.Request) -> web.Response:
        return await self._set_active(request, False)

    async def enable(self, request: web.Request) -> web.Response:
        return await self._set_active(request, True)

    async def _set_active(
        self, request: web.Request, active: bool
    ) -> web.Response:
        """Turn an account on or off, as a POST with a token.

        Refuses to act on the account that is asking: an administrator who
        disables themselves locks themselves out of the only place that could
        undo it, and the CLI would be the way back. The flag survives, so
        re-enabling from the host restores everything, but a mistake here
        should not be the thing that does it.

        Nothing revokes sessions. A disabled account stops working on its
        next request, because that is checked on the way in — there is
        nothing to revoke.
        """
        if not self._csrf.accepts(
            request.cookies.get(SESSION_COOKIE, ""),
            _form_value(await request.post(), CSRF_FIELD),
        ):
            return form_forged()

        actor = resolve_user(request, request.app[USERS], request.app[SESSIONS])
        target_id = int(request.match_info["user_id"])

        if actor is not None and actor.id == target_id:
            return redirect(USERS_PATH)

        account = self._users.get_by_id(target_id)

        if account is not None:
            self._users.set_active(account.id, active)

            logger.info(
                "%s web account %r",
                "Enabled" if active else "Disabled",
                account.username,
            )

        return redirect(USERS_PATH)

    def _page(
        self, found: list[User], query: str, request: web.Request
    ) -> web.Response:
        """The accounts page, with a form per account to change its state.

        A form per row rather than one form with a select: a table of people
        and one button each is something you can read without decoding a
        dropdown, and the row you are looking at is the row you are acting on.
        """
        source = (TEMPLATES_DIR / "admin_users.html").read_text(encoding="utf-8")
        token = html.escape(
            self._csrf.session_token(request.cookies.get(SESSION_COOKIE, ""))
        )
        actor = resolve_user(request, request.app[USERS], request.app[SESSIONS])

        source = source.replace("__ADMIN_STYLES__", _admin_styles())
        source = source.replace("__MENU_STYLES__", menu_styles())
        source = source.replace("__NAV__", self._nav_for(request))
        source = source.replace("__QUERY__", html.escape(query, quote=True))
        source = source.replace("__ROWS__", _rows(found, token, actor))

        return web.Response(text=source, content_type="text/html", charset="utf-8")


def _rows(found: list[User], token: str, actor: User | None) -> str:
    """One table of accounts, or a line saying there are none."""
    if not found:
        return '<p class="none">Ничего не найдено.</p>'

    body = []

    for user in found:
        # The button an administrator may not press on their own account. Left
        # out rather than disabled so the table reads the same for every row
        # and the reason is the one the handler would have given.
        target = "disable" if user.is_active else "enable"
        label = "Отключить" if user.is_active else "Включить"
        danger = ' class="danger"' if user.is_active else ""
        button = ""

        if actor is None or actor.id != user.id:
            button = (
                f'<form method="post" action="{USERS_PATH}'
                f'/{user.id}/{target}">'
                f'<input type="hidden" name="{CSRF_FIELD}" value="{token}">'
                f'<button type="submit"{danger}>{label}</button>'
                "</form>"
            )

        state = "on" if user.is_active else "off"
        mark = "Админ" if user.is_admin else ""

        body.append(
            "<tr>"
            f'<td class="name">{html.escape(user.username)}</td>'
            f'<td><span class="badge {state}">'
            f'{"активен" if user.is_active else "отключён"}</span></td>'
            f'<td class="mark">{mark}</td>'
            f'<td class="when">{_when(user.created_at)}</td>'
            f"<td>{button}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr>"
        "<th>Имя</th><th>Состояние</th><th>Права</th><th>Создан</th><th></th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _when(moment: datetime) -> str:
    return moment.strftime("%d.%m.%Y")


def _form_value(form, name: str) -> str:
    value = form.get(name, "")

    return value if isinstance(value, str) else ""


def _admin_styles() -> str:
    return (TEMPLATES_DIR / "_admin_styles.html").read_text(encoding="utf-8")
