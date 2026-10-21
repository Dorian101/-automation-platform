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
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiohttp import web

from app.core.config import Config
from app.db.database import Database
from app.db.sessions_repo import SessionsRepository
from app.db.users_repo import User, UsersRepository
from app.db.visits_repo import DEFAULT_LIMIT, Visit, VisitsRepository
from app.web.auth import SESSION_COOKIE, resolve_user
from app.web.csrf import CSRF_FIELD, CsrfSigner
from app.web.pages import menu_styles
from app.web.responses import redirect, token_rejected
from app.web.system import cores, disk, load, memory

logger = logging.getLogger(__name__)

ADMIN_PREFIX = "/admin"
USERS_PATH = ADMIN_PREFIX + "/users"
STATUS_PATH = ADMIN_PREFIX + "/status"

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
        visits: VisitsRepository,
        database: Database,
        nav_for: Callable[[web.Request], str],
        csrf: CsrfSigner,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._visits = visits
        self._database = database
        # The same signer as the rest of the web layer. A second one with its
        # own secret would refuse every token the other half produced, and the
        # failure would look like a forgery rather than a wiring mistake.
        self._csrf = csrf
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
        sub.router.add_get("/visits", self.visits_page)
        sub.router.add_get("/status", self.status_page)
        sub.router.add_get("/status/data", self.status_data)
        sub.router.add_post("/users/{user_id:\\d+}/disable", self.disable)
        sub.router.add_post("/users/{user_id:\\d+}/enable", self.enable)

        app.add_subapp(ADMIN_PREFIX, sub)

    async def index(self, request: web.Request) -> web.Response:
        """`/admin` leads to the status page, the overview.

        The area opened on the user list when that was the only thing in it.
        Now there are three sections and the overview is the front door: it is
        the page that answers "is the platform healthy" without a click.
        """
        return redirect(STATUS_PATH)

    async def users_page(self, request: web.Request) -> web.Response:
        """The accounts, optionally narrowed to those matching `q`.

        A search is a GET: it changes nothing, and an address that can be
        bookmarked and sent to somebody is worth more here than a POST that
        cannot be.
        """
        query = request.query.get("q", "").strip()

        found = self._users.search(query)
        actor = resolve_user(request, request.app[USERS], request.app[SESSIONS])
        token = html.escape(
            self._csrf.session_token(request.cookies.get(SESSION_COOKIE, ""))
        )

        body = _fragment("admin_users_body.html", {
            "__QUERY__": html.escape(query, quote=True),
            "__ROWS__": _user_rows(found, token, actor),
        })

        return self._page("users", body, request)

    async def visits_page(self, request: web.Request) -> web.Response:
        """The event log, newest first, optionally narrowed to one page.

        `hide=1` drops the administrator's own events. Not because they are
        noise in themselves — but the owner reads this page to find out
        whether anybody else came, and their own checking is recorded here
        exactly like a stranger's, because nothing in the log says which is
        which.

        The log is read a page at a time, and the page number is `page=2`.
        The exclusion is asked for in the query rather than applied to the
        rows afterwards, so that the count and the table are cut by the same
        line: a total that included what the table hides is a number nobody
        can match against the table under it.
        """
        query = request.query.get("q", "").strip()
        hiding = request.query.get("hide") == "1"

        # Who the owner is does not change from one row to the next, and
        # finding out is two queries. Asking per row cost the page four
        # seconds once the log had a few hundred entries in it.
        mine = _mine(request) if hiding else None
        exclude = mine if hiding else None

        total = self._visits.count(path=query, exclude=exclude)
        whole = total if not query else self._visits.count(exclude=exclude)
        pages = max(1, -(-total // DEFAULT_LIMIT))
        page = min(_page_number(request.query.get("page", "")), pages)
        offset = (page - 1) * DEFAULT_LIMIT

        found = self._visits.recent(path=query, offset=offset, exclude=exclude)

        body = _fragment("admin_visits.html", {
            "__QUERY__": html.escape(query, quote=True),
            "__PATH_OPTIONS__": "".join(
                f'<option value="{html.escape(path, quote=True)}">'
                for path in self._visits.paths()
            ),
            "__CHECKED__": " checked" if hiding else "",
            "__COUNT_LINE__": _count_line(total, whole, len(found), offset),
            "__PAGER__": _pager(page, pages, query, hiding),
            "__ROWS__": _visit_rows(found),
        })

        return self._page("visits", body, request)

    async def status_page(self, request: web.Request) -> web.Response:
        """The overview: resources, the database, and the backups.

        Rendered once here; the numbers themselves ride a separate JSON
        endpoint below, which a small script refreshes so the page does not
        have to be reloaded to see the disk tick. The placeholder text is all
        an administrator sees for the moment before the first fetch lands.
        """
        body = _fragment("admin_status.html", {})
        return self._page("status", body, request)

    async def status_data(self, request: web.Request) -> web.Response:
        """The status page's numbers, as JSON.

        One endpoint for the whole page rather than one per figure: the page is
        three groups of numbers, and a request per group would be three round
        trips for what a single file can carry. The database counts are asked
        in one pass and the system figures are already cheap.
        """
        return web.json_response(_status_snapshot(self._database, self._visits))

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
            return token_rejected(request, USERS_PATH)

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
        self, section: str, body: str, request: web.Request
    ) -> web.Response:
        """One page of the area, in the shell the sections share.

        The shell exists now that there are two sections. With one it would
        have been a wrapper around a single document.
        """
        source = (TEMPLATES_DIR / "admin.html").read_text(encoding="utf-8")

        source = source.replace("__ADMIN_STYLES__", _admin_styles())
        source = source.replace("__MENU_STYLES__", menu_styles())
        source = source.replace("__NAV__", self._nav_for(request))
        source = source.replace("__BODY__", body)
        source = source.replace("__TITLE__", _section_title(section))

        for name in ("status", "users", "visits"):
            mark = " aria-current='page'" if name == section else ""
            source = source.replace(f"__ACTIVE_{name.upper()}__", mark)

        return web.Response(text=source, content_type="text/html", charset="utf-8")


def _section_title(section: str) -> str:
    return {"status": "Состояние", "users": "Пользователи", "visits": "Визиты"}[section]


#: The data tables whose row counts the status page shows. Names as a person
#: reads them, not as the migrations write them: the point of the figure is
#: "how much is in here", and `users` reads better than the sql file's table.
_STATUS_TABLES = [
    ("notes", "Заметки"),
    ("reminders", "Напоминания"),
    ("clipboard", "Буфер"),
    ("expenses", "Расходы"),
    ("job_applications", "Заявки"),
    ("users", "Аккаунты"),
    ("visits", "Визиты"),
]


def _status_snapshot(database: Database, visits: VisitsRepository) -> dict:
    """Everything the status page shows, in one dict.

    The database counts are one `SELECT count(*)` each rather than joined,
    which keeps the figures independent: one table being slow does not take the
    rest down with it.
    """
    return {
        "tables": _table_counts(database),
        "backups": _backups(),
        "system": {
            "disk": disk(),
            "memory": memory(),
            "load": load(),
            "cores": cores(),
        },
        "last_visit": _last_visit(visits),
    }


def _table_counts(database: Database) -> dict:
    counts = {}

    with database.connect() as conn:
        with conn.cursor() as cur:
            for table, label in _STATUS_TABLES:
                cur.execute(f'SELECT count(*) FROM "{table}"')
                counts[label] = cur.fetchone()[0]

    return counts


def _last_visit(visits: VisitsRepository) -> str | None:
    """When somebody last arrived, in DISPLAY_TIMEZONE, or None.

    The newest row is enough; the full log is the `/admin/visits` page's job.
    Formatted like the backup timestamp below — `%d.%m.%Y %H:%M` — so it reads
    on the wall clock without an offset suffix that only lengthens the line.
    """
    rows = visits.recent(limit=1)

    if not rows:
        return None

    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    return (
        rows[0].at.replace(tzinfo=UTC)
        .astimezone(zone)
        .strftime("%d.%m.%Y %H:%M")
    )


def _backups() -> dict:
    """The newest backup and how much of the reply directory is taken.

    Nothing here writes or prunes — that is the timer's job. It only reports,
    so an administrator can see at a glance that dumps are still happening and
    that they are not about to fill the disk.
    """
    from app.db.backup import BackupManager

    manager = BackupManager()
    directory = manager.backups_dir

    files = sorted(directory.glob("*.sql"), key=lambda p: p.stat().st_mtime) \
        if directory.is_dir() else []

    newest = files[-1] if files else None

    newest_at = None
    if newest is not None:
        zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
        newest_at = (
            datetime.fromtimestamp(newest.stat().st_mtime, tz=UTC)
            .astimezone(zone)
            .strftime("%d.%m.%Y %H:%M")
        )

    return {
        "count": len(files),
        "newest": newest.name if newest else None,
        "newest_at": newest_at,
        "total_bytes": sum(p.stat().st_size for p in files),
    }


def _user_rows(found: list[User], token: str, actor: User | None) -> str:
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


def _mine(request: web.Request) -> str | None:
    """The identity the log files the administrator under, or None.

    Through the repository's own conversion rather than by formatting the
    account here. `User` is a dataclass, so `str()` on one gives its repr and
    not its identity — and a filter comparing against that hides nobody while
    looking like it works.
    """
    users = request.app[USERS]
    actor = resolve_user(request, users, request.app[SESSIONS])

    return str(users.identity_for(actor)) if actor is not None else None


def _count_line(total: int, whole: int, shown: int, offset: int) -> str:
    """What the count above the table is counting.

    Three numbers and all of them are true: how many events the log holds,
    how many of them the filter keeps, and which of those are on the screen.
    The first two used to disagree in a way nobody could match against the
    table under it — the total counted the owner's own visits while the table
    hid them — and a total nobody can match against the table is a small
    thing to wonder about every time.

    The filter's number only appears when it differs, because a page with no
    filter has one total and not two of the same.
    """
    parts = [f"Всего событий: <b>{whole}</b>"]

    if total != whole:
        parts.append(f"под фильтром: <b>{total}</b>")

    if total:
        parts.append(f"показано <b>{offset + 1}–{offset + shown}</b>")

    return " &middot; ".join(parts)


def _page_number(raw: str) -> int:
    """The page asked for, or the first one.

    A page number is a thing a person types, so it is checked rather than
    trusted: anything that is not a whole number of pages, or is not one at
    all, is the first page. A number past the last page is not fixed here —
    the caller knows how many pages there are and clamps it to the last, so
    that a stale link lands on real rows instead of an empty table.
    """
    try:
        number = int(raw)
    except ValueError:
        return 1

    return number if number >= 1 else 1


def _visit_url(page: int, query: str, hiding: bool) -> str:
    """The address of one page of the log, with the filters still on.

    The page number is left out of the first page's address, because a link
    to the first page is the page itself and not a page of something.
    """
    arguments: dict[str, str] = {}

    if query:
        arguments["q"] = query

    if hiding:
        arguments["hide"] = "1"

    if page > 1:
        arguments["page"] = str(page)

    encoded = urlencode(arguments)

    return f"/admin/visits?{encoded}" if encoded else "/admin/visits"


def _pager(page: int, pages: int, query: str, hiding: bool) -> str:
    """The way to the other pages, and the way back.

    The filters travel with it: a reader who narrowed the log and then turned
    the page expects to still be looking at the same narrowing, and a pager
    that dropped it would answer a question nobody asked.
    """
    parts = []

    if page > 1:
        earlier = html.escape(
            _visit_url(page - 1, query, hiding), quote=True
        )
        parts.append(f'<a class="pagerlink" href="{earlier}">&larr; раньше</a>')

    parts.append(f"<span>страница {page} из {pages}</span>")

    if page < pages:
        later = html.escape(_visit_url(page + 1, query, hiding), quote=True)
        parts.append(f'<a class="pagerlink" href="{later}">позже &rarr;</a>')

    return '<nav class="pager">' + " ".join(parts) + "</nav>"


def _fragment(template: str, replacements: dict[str, str]) -> str:
    """Read one of the area's page bodies and fill in its placeholders."""
    source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")

    for name, value in replacements.items():
        source = source.replace(name, value)

    return source


def _visit_rows(found: list[Visit]) -> str:
    """The log, newest first."""
    if not found:
        return '<p class="none">Пока ничего не записано.</p>'

    # The column was written by CURRENT_TIMESTAMP on a UTC host and carries no
    # offset, so what arrives here is UTC and printing it as-is reads three
    # hours early to anybody not on that clock. Naming the zone in the heading
    # as well is the cheap half of the fix: the other half is that a reader
    # cannot tell which clock a bare timestamp is on.
    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    labels = {"view": "просмотр", "signin": "вход", "signup": "регистрация"}
    body = []

    for visit in found:
        who = html.escape(visit.identity) if visit.identity else "&mdash;"
        # The origin behind the visit, or a dash. A referrer is a URL up to the
        # host only, never the full path, so what is printed cannot carry a
        # search query. It is escaped like the path: it came from a header, so
        # it is data, not markup.
        from_where = (
            html.escape(visit.referrer) if visit.referrer else "&mdash;"
        )
        when = visit.at.replace(tzinfo=UTC).astimezone(zone)
        body.append(
            "<tr>"
            f'<td class="when">{when.strftime("%d.%m.%Y %H:%M:%S")}</td>'
            f'<td>{labels.get(visit.kind, html.escape(visit.kind))}</td>'
            f'<td class="name">{html.escape(visit.path)}</td>'
            f'<td class="when">{from_where}</td>'
            f"<td>{who}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr>"
        f"<th>Когда, {html.escape(datetime.now(zone).tzname() or '')}</th>"
        "<th>Событие</th><th>Страница</th><th>Откуда</th><th>Кто</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )
