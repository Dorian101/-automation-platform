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

import asyncio
import html
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiohttp import web

from app.core.config import Config
from app.core.identity import Identity
from app.db.backup import BackupManager
from app.db.database import Database
from app.db.errors_repo import AppError, ErrorsRepository
from app.db.feedback_repo import Feedback, FeedbackRepository
from app.db.sessions_repo import (
    ACTIVE_MINUTES,
    ActiveSession,
    SessionsRepository,
    digest,
)
from app.db.settings_repo import (
    ALWAYS_ON,
    DEMO_PER_HOUR,
    DISABLED_PLUGINS,
    FEEDBACK_PER_HOUR,
    MAINTENANCE,
    SIGNUP_OPEN,
    SettingsRepository,
)
from app.db.users_repo import User, UsersRepository
from app.db.visits_repo import DEFAULT_LIMIT, Visit, VisitsRepository
from app.plugins.tables import data_tables, demo_tables
from app.web.auth import SESSION_COOKIE, resolve_user
from app.web.csrf import CSRF_FIELD, CsrfSigner
from app.web.demo import PRUNED_AT, DemoService
from app.web.pages import menu_styles, render_not_found
from app.web.responses import redirect, token_rejected
from app.web.system import cores, disk, load, memory

logger = logging.getLogger(__name__)

ADMIN_PREFIX = "/admin"
USERS_PATH = ADMIN_PREFIX + "/users"
STATUS_PATH = ADMIN_PREFIX + "/status"
SETTINGS_PATH = ADMIN_PREFIX + "/settings"
BROADCAST_PATH = ADMIN_PREFIX + "/broadcast"
VISITS_PATH = ADMIN_PREFIX + "/visits"
SESSIONS_PATH = ADMIN_PREFIX + "/sessions"
FEEDBACK_PATH = ADMIN_PREFIX + "/feedback"
ERRORS_PATH = ADMIN_PREFIX + "/errors"

#: The longest announcement: one Telegram message.
BROADCAST_LIMIT = 4096

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

USERS: web.AppKey[UsersRepository] = web.AppKey("users", UsersRepository)
SESSIONS: web.AppKey[SessionsRepository] = web.AppKey(
    "sessions", SessionsRepository
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
            text=render_not_found(),
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
        settings: SettingsRepository | None = None,
        manager=None,
        notifier=None,
        *,
        changed: Callable[..., None],
    ) -> None:
        self._changed = changed
        self._users = users
        self._sessions = sessions
        self._visits = visits
        self._database = database
        self._feedback = FeedbackRepository(database)
        self._errors = ErrorsRepository(database)
        # The same switches the web server and the bot read, so a change here
        # applies at once rather than after the cache runs out.
        self._settings = settings or SettingsRepository(database)
        self._manager = manager
        self._notifier = notifier
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
        sub.router.add_get("/sessions", self.sessions_page)
        sub.router.add_get("/feedback", self.feedback_page)
        sub.router.add_get("/errors", self.errors_page)
        sub.router.add_get("/broadcast", self.broadcast_page)
        sub.router.add_post("/broadcast", self.broadcast_send)
        sub.router.add_get("/settings", self.settings_page)
        sub.router.add_post("/settings", self.settings_save)
        sub.router.add_post("/tasks/{task:backup|guests|visits}", self.run_task)
        sub.router.add_post("/logins/{session_id:\\d+}/end", self.end_login)
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
        body += _login_rows(
            self._sessions.active(),
            token,
            digest(request.cookies.get(SESSION_COOKIE, "")),
        )

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

    async def sessions_page(self, request: web.Request) -> web.Response:
        """The log read as people rather than rows: one line per browser visit.

        Bots are hidden unless asked for, because a link sent in a chat makes
        the messenger fetch it, and those fetches would outnumber readers.
        """
        showing_bots = request.query.get("bots") == "1"
        hiding = request.query.get("hide") == "1"
        mine = _mine(request) if hiding else None

        found = [
            session
            for session in _sessions(self._visits.with_visitor(SESSION_DAYS))
            if (showing_bots or not session.bot)
            and (mine is None or mine not in session.identities)
        ]
        found.reverse()

        pages = max(1, -(-len(found) // SESSIONS_PER_PAGE))
        page = min(_page_number(request.query.get("page", "")), pages)
        offset = (page - 1) * SESSIONS_PER_PAGE
        shown = found[offset:offset + SESSIONS_PER_PAGE]

        arguments = {"bots": "1"} if showing_bots else {}
        if hiding:
            arguments["hide"] = "1"

        body = _fragment("admin_sessions.html", {
            "__BOTS__": " checked" if showing_bots else "",
            "__CHECKED__": " checked" if hiding else "",
            "__GAP__": str(SESSION_GAP_MINUTES),
            "__COUNT_LINE__": (
                f"Сессий за {SESSION_DAYS} дней: <b>{len(found)}</b>"
                + (f" &middot; показано <b>{offset + 1}–{offset + len(shown)}</b>"
                   if shown else "")
            ),
            "__PAGER__": _sessions_pager(page, pages, arguments),
            "__ROWS__": _session_rows(shown),
        })

        return self._page("sessions", body, request)

    def _token_ok(self, request: web.Request, form) -> bool:
        return self._csrf.accepts(
            request.cookies.get(SESSION_COOKIE, ""), _form_value(form, CSRF_FIELD)
        )

    def _token(self, request: web.Request) -> str:
        return html.escape(
            self._csrf.session_token(request.cookies.get(SESSION_COOKIE, ""))
        )

    async def settings_page(self, request: web.Request) -> web.Response:
        """The switches: maintenance, registration, plugins and form limits."""
        plugins = self._manager.get_plugins() if self._manager else []
        disabled = self._settings.disabled_plugins()
        boxes = "".join(
            '<label class="toggle">'
            f'<input type="checkbox" name="plugin" '
            f'value="{html.escape(plugin.name, quote=True)}"'
            f'{"" if plugin.name in disabled else " checked"}> '
            f"{html.escape(plugin.description or plugin.name)} "
            f'<span class="mark">{html.escape(plugin.name)}</span></label>'
            for plugin in plugins
            if plugin.name not in ALWAYS_ON
        )

        body = _fragment("admin_settings.html", {
            "__FLASH__": (
                '<p class="flash">Сохранено.</p>'
                if request.query.get("saved") == "1" else ""
            ),
            "__CSRF_FIELD__": CSRF_FIELD,
            "__CSRF__": self._token(request),
            "__MAINTENANCE__": " checked" if self._settings.maintenance() else "",
            "__SIGNUP__": " checked" if self._settings.signup_open() else "",
            "__PLUGINS__": boxes or '<p class="none">Плагинов нет.</p>',
            "__FEEDBACK__": str(self._settings.feedback_per_hour()),
            "__DEMO__": str(self._settings.demo_per_hour()),
        })

        return self._page("settings", body, request)

    async def settings_save(self, request: web.Request) -> web.Response:
        """Store the form. A number out of range keeps the value it replaces."""
        form = await request.post()

        if not self._token_ok(request, form):
            return token_rejected(request, SETTINGS_PATH)

        self._settings.put(MAINTENANCE, "1" if form.get("maintenance") else "0")
        self._settings.put(SIGNUP_OPEN, "1" if form.get("signup") else "0")

        if self._manager is not None:
            kept = set(form.getall("plugin", []))
            off = sorted(
                plugin.name
                for plugin in self._manager.get_plugins()
                if plugin.name not in kept and plugin.name not in ALWAYS_ON
            )
            self._settings.put(DISABLED_PLUGINS, ",".join(off) or None)

        for key, field_name, low, high in (
            (FEEDBACK_PER_HOUR, "feedback_per_hour", 1, 100),
            (DEMO_PER_HOUR, "demo_per_hour", 10, 10000),
        ):
            raw = _form_value(form, field_name).strip()
            if raw.isdigit() and low <= int(raw) <= high:
                self._settings.put(key, raw)

        actor = resolve_user(request, request.app[USERS], request.app[SESSIONS])
        logger.info(
            "Settings changed by %r", actor.username if actor else None
        )

        return redirect(SETTINGS_PATH + "?saved=1")

    async def errors_page(self, request: web.Request) -> web.Response:
        """The application's recent ERROR records, newest first."""
        return self._page("errors", _error_rows(self._errors.recent()), request)

    def _broadcast_targets(self) -> list[Identity]:
        if self._notifier is None:
            return []

        targets = (
            self._users.identity_for(account)
            for account in self._users.list_all()
            if account.is_active
        )

        return [target for target in targets if self._notifier.reachable(target)]

    async def broadcast_page(self, request: web.Request) -> web.Response:
        flash = ""
        sent, of = request.query.get("sent", ""), request.query.get("of", "")

        if sent.isdigit() and of.isdigit():
            flash = f'<p class="flash">Отправлено: <b>{sent}</b> из <b>{of}</b>.</p>'

        body = _fragment("admin_broadcast.html", {
            "__FLASH__": flash,
            "__COUNT__": str(len(self._broadcast_targets())),
            "__CSRF_FIELD__": CSRF_FIELD,
            "__CSRF__": self._token(request),
            "__MAX__": str(BROADCAST_LIMIT),
        })

        return self._page("broadcast", body, request)

    async def broadcast_send(self, request: web.Request) -> web.Response:
        form = await request.post()

        if not self._token_ok(request, form):
            return token_rejected(request, BROADCAST_PATH)

        text = _form_value(form, "text").strip()[:BROADCAST_LIMIT]
        targets = self._broadcast_targets()
        sent = 0

        if text:
            for target in targets:
                try:
                    if await self._notifier.deliver(target, text):
                        sent += 1
                except Exception:
                    logger.exception("Announcement to %s failed", target)
                await asyncio.sleep(0.05)

        logger.info("Announcement sent to %s of %s account(s)", sent, len(targets))

        return redirect(f"{BROADCAST_PATH}?sent={sent}&of={len(targets)}")

    async def run_task(self, request: web.Request) -> web.Response:
        """Run one of the scheduled jobs now: a backup or a cleanup."""
        form = await request.post()

        if not self._token_ok(request, form):
            return token_rejected(request, STATUS_PATH)

        task = request.match_info["task"]
        count = 0

        try:
            if task == "backup":
                await asyncio.to_thread(BackupManager().create_backup, "manual")
            elif task == "guests":
                removed = await asyncio.to_thread(
                    DemoService(
                        self._manager, self._database, settings=self._settings
                    ).prune
                )
                count = sum(removed.values())
            else:
                count = await asyncio.to_thread(self._visits.prune)
        except Exception:
            logger.exception("Task %s failed from the admin area", task)
            return redirect(f"{STATUS_PATH}?failed={task}")

        logger.info("Task %s run from the admin area", task)

        return redirect(f"{STATUS_PATH}?done={task}&n={count}")

    async def end_login(self, request: web.Request) -> web.Response:
        """Sign one session out. Ending your own signs you out here too."""
        form = await request.post()

        if not self._token_ok(request, form):
            return token_rejected(request, USERS_PATH)

        if self._sessions.delete_by_id(int(request.match_info["session_id"])):
            logger.info("Session ended from the admin area")
            self._changed(USERS_PATH)

        return redirect(USERS_PATH)

    async def feedback_page(self, request: web.Request) -> web.Response:
        """What readers left in the guest book on the public pages."""
        return self._page("feedback", _feedback_rows(self._feedback.recent()), request)

    async def status_page(self, request: web.Request) -> web.Response:
        """The overview: resources, the database, and the backups.

        Rendered once here; the numbers themselves ride a separate JSON
        endpoint below, which a small script refreshes so the page does not
        have to be reloaded to see the disk tick. The placeholder text is all
        an administrator sees for the moment before the first fetch lands.
        """
        token = html.escape(
            self._csrf.session_token(request.cookies.get(SESSION_COOKIE, ""))
        )
        body = (
            _task_flash(request.query)
            + _fragment("admin_status.html", {})
            + _task_buttons(token)
            + _demo_block(self._database, self._settings.get(PRUNED_AT))
        )
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
            self._changed(USERS_PATH)

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

        for name in (
            "status", "users", "visits", "sessions", "feedback", "errors",
            "broadcast", "settings",
        ):
            mark = " aria-current='page'" if name == section else ""
            source = source.replace(f"__ACTIVE_{name.upper()}__", mark)

        return web.Response(text=source, content_type="text/html", charset="utf-8")


def _section_title(section: str) -> str:
    return {
        "status": "Состояние",
        "users": "Пользователи",
        "visits": "Визиты",
        "sessions": "Сессии",
        "feedback": "Отзывы",
        "errors": "Ошибки",
        "broadcast": "Рассылка",
        "settings": "Настройки",
    }[section]


_CORE_TABLES = [
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
            for table, label in data_tables() + _CORE_TABLES:
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


def _feedback_rows(found: list[Feedback]) -> str:
    """The guest book, newest first. The text is a stranger's, so it is
    escaped, and its line breaks are kept by the stylesheet, not by markup."""
    if not found:
        return '<p class="none">Отзывов пока нет.</p>'

    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    body = []

    for entry in found:
        when = entry.at.replace(tzinfo=UTC).astimezone(zone)
        page = html.escape(entry.page) if entry.page else "&mdash;"
        body.append(
            "<tr>"
            f'<td class="when">{when.strftime("%d.%m.%Y %H:%M")}</td>'
            f'<td class="when">{page}</td>'
            f'<td class="feedback-text">{html.escape(entry.text)}</td>'
            "</tr>"
        )

    return (
        "<table><thead><tr>"
        f"<th>Когда, {html.escape(datetime.now(zone).tzname() or '')}</th>"
        "<th>Страница</th><th>Текст</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


#: Visits from one browser further apart than this start a new session. The
#: usual web analytics figure: long enough to read a page, short enough that
#: coming back after lunch is a second visit.
SESSION_GAP_MINUTES = 30
#: How far back the sessions view reads. The hash only exists from the day it
#: was introduced, and a month of sessions is more than a screen anyway.
SESSION_DAYS = 30
SESSIONS_PER_PAGE = 30


@dataclass
class Session:
    visits: list[Visit] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.visits[0].at

    @property
    def end(self) -> datetime:
        return self.visits[-1].at

    @property
    def bot(self) -> bool:
        return all(visit.bot for visit in self.visits)

    @property
    def identities(self) -> set[str]:
        return {visit.identity for visit in self.visits if visit.identity}


def _sessions(visits: list[Visit]) -> list[Session]:
    """Group visits, oldest first, into sessions, oldest first.

    One browser's visits belong together while the gaps between them stay
    under `SESSION_GAP_MINUTES`. The hash already changes at midnight UTC, so
    a session never spans two days' salts.
    """
    gap = timedelta(minutes=SESSION_GAP_MINUTES)
    open_sessions: dict[str, Session] = {}
    done: list[Session] = []

    for visit in visits:
        current = open_sessions.get(visit.visitor)

        if current is None or visit.at - current.end > gap:
            current = Session()
            open_sessions[visit.visitor] = current
            done.append(current)

        current.visits.append(visit)

    return done


def _duration(session: Session) -> str:
    minutes = int((session.end - session.start).total_seconds() // 60)

    return "&lt;1 мин" if minutes < 1 else f"{minutes} мин"


def _route(session: Session) -> str:
    """The pages in the order they were read, repeats in a row collapsed."""
    paths: list[str] = []

    for visit in session.visits:
        if not paths or paths[-1] != visit.path:
            paths.append(visit.path)

    return " &rarr; ".join(html.escape(path) for path in paths)


def _source(session: Session) -> str:
    """Where the session came from: the link's tag, else the referring site."""
    first = session.visits[0]

    if first.campaign:
        return f'<span class="badge on">{html.escape(first.campaign)}</span>'

    if first.referrer:
        return html.escape(first.referrer.split("://", 1)[-1])

    return "&mdash;"


def _who(session: Session) -> str:
    """Accounts named in the session; a demo guest's random token is just a guest."""
    names = sorted(
        "гость демо" if name.startswith("web:guest:") else name
        for name in session.identities
    )

    return html.escape(", ".join(dict.fromkeys(names))) if names else "&mdash;"


def _session_rows(found: list[Session]) -> str:
    if not found:
        return '<p class="none">Пока ничего не записано.</p>'

    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    body = []

    for session in found:
        start = session.start.replace(tzinfo=UTC).astimezone(zone)
        agent = html.escape(session.visits[0].agent or "—")
        if session.bot:
            agent += ' <span class="badge off">бот</span>'

        body.append(
            "<tr>"
            f'<td class="when">{start.strftime("%d.%m.%Y %H:%M")}</td>'
            f'<td class="when">{_duration(session)}</td>'
            f"<td>{agent}</td>"
            f"<td>{_source(session)}</td>"
            f'<td class="name">{_route(session)}</td>'
            f"<td>{_who(session)}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr>"
        f"<th>Начало, {html.escape(datetime.now(zone).tzname() or '')}</th>"
        "<th>Длительность</th><th>Устройство</th><th>Откуда</th>"
        "<th>Путь</th><th>Кто</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _sessions_pager(page: int, pages: int, arguments: dict[str, str]) -> str:
    def url(number: int) -> str:
        query = dict(arguments)
        if number > 1:
            query["page"] = str(number)
        encoded = urlencode(query)
        return html.escape(
            "/admin/sessions" + (f"?{encoded}" if encoded else ""), quote=True
        )

    parts = []

    if page > 1:
        parts.append(f'<a class="pagerlink" href="{url(page - 1)}">&larr; новее</a>')

    parts.append(f"<span>страница {page} из {pages}</span>")

    if page < pages:
        parts.append(f'<a class="pagerlink" href="{url(page + 1)}">старше &rarr;</a>')

    return '<nav class="pager">' + " ".join(parts) + "</nav>"


_TASK_DONE = {
    "backup": "Бэкап записан.",
    "guests": "Гости демо удалены, строк: {n}.",
    "visits": "Старые визиты удалены, строк: {n}.",
}


def _task_flash(query) -> str:
    """The line above the status page after a task button was pressed."""
    done, failed = query.get("done", ""), query.get("failed", "")
    count = query.get("n", "0")

    if done in _TASK_DONE:
        text = _TASK_DONE[done].format(n=int(count) if count.isdigit() else 0)
        return f'<p class="flash">{text}</p>'

    if failed in _TASK_DONE:
        return (
            '<p class="flash error" role="alert">Задача не выполнилась, '
            "подробности в «Ошибках».</p>"
        )

    return ""


def _task_buttons(token: str) -> str:
    buttons = "".join(
        f'<form method="post" action="{STATUS_PATH.rsplit("/", 1)[0]}/tasks/{task}">'
        f'<input type="hidden" name="{CSRF_FIELD}" value="{token}">'
        f'<button type="submit">{label}</button></form>'
        for task, label in (
            ("backup", "Сделать бэкап сейчас"),
            ("guests", "Удалить гостей демо"),
            ("visits", "Удалить визиты старше 90 дней"),
        )
    )

    return f'<h2>Служебные задачи</h2><div class="task-buttons">{buttons}</div>'


def _demo_block(database: Database, pruned_at: str | None) -> str:
    """How much the demo holds now: guests and their rows per table."""
    pattern = "web:guest:%"
    tables = demo_tables()
    counts = []
    guests = 0

    with database.connect() as conn:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(
                    f"SELECT count(*) FROM {table} WHERE user_id LIKE %s",
                    (pattern,),
                )
                counts.append((table, cur.fetchone()[0]))

            if tables:
                union = " UNION ".join(
                    f"SELECT user_id FROM {table} WHERE user_id LIKE %s"
                    for table in tables
                )
                cur.execute(
                    f"SELECT count(*) FROM ({union}) AS guests",
                    (pattern,) * len(tables),
                )
                guests = cur.fetchone()[0]

    items = "".join(
        f"<li><span>{html.escape(table)}</span><b>{count}</b></li>"
        for table, count in counts
    )
    when = (
        f"{html.escape(pruned_at)} UTC" if pruned_at
        else "ещё не было с момента обновления"
    )

    return (
        "<h2>Демо</h2>"
        f'<p class="legend">Гостей сейчас: <b>{guests}</b>. Очистка — '
        f"каждый день в 05:23 UTC, последняя: {when}.</p>"
        f'<ul class="count-list">{items}</ul>'
    )


def _login_rows(found: list[ActiveSession], token: str, mine: str) -> str:
    """Sign-ins in use now, with a button to end each one."""
    legend = (
        f'<p class="legend">Входы, которыми пользовались за последние '
        f"{ACTIVE_MINUTES} минут. Закрытая вкладка пропадает отсюда через "
        "полчаса, хотя сам вход действует до конца срока cookie.</p>"
    )

    if not found:
        return (
            "<h2>Активные входы</h2>" + legend
            + '<p class="none">Сейчас никто не активен.</p>'
        )

    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    body = []

    for session in found:
        seen = session.last_seen.replace(tzinfo=UTC).astimezone(zone)
        started = session.created_at.replace(tzinfo=UTC).astimezone(zone)
        you = (
            ' <span class="badge on">это вы</span>'
            if session.token_hash == mine else ""
        )
        body.append(
            "<tr>"
            f'<td class="name">{html.escape(session.username)}{you}</td>'
            f"<td>{html.escape(session.agent or '—')}</td>"
            f'<td class="when">{started.strftime("%d.%m.%Y %H:%M")}</td>'
            f'<td class="when">{seen.strftime("%H:%M")}</td>'
            f'<td><form method="post" action="{ADMIN_PREFIX}/logins/{session.id}/end">'
            f'<input type="hidden" name="{CSRF_FIELD}" value="{token}">'
            '<button type="submit" class="danger">Завершить</button></form></td>'
            "</tr>"
        )

    return (
        "<h2>Активные входы</h2>" + legend + "<table><thead><tr>"
        "<th>Аккаунт</th><th>Устройство</th>"
        f"<th>Вход, {html.escape(datetime.now(zone).tzname() or '')}</th>"
        "<th>Последняя активность</th><th></th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _error_rows(found: list[AppError]) -> str:
    if not found:
        return '<h1>Ошибки</h1><p class="none">Ошибок не было.</p>'

    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    body = []

    for error in found:
        at = error.at.replace(tzinfo=UTC).astimezone(zone)
        details = (
            f"<details><summary>трассировка</summary>"
            f"<pre>{html.escape(error.details)}</pre></details>"
            if error.details else ""
        )
        body.append(
            "<tr>"
            f'<td class="when">{at.strftime("%d.%m.%Y %H:%M:%S")}</td>'
            f'<td class="when">{html.escape(error.logger)}</td>'
            f'<td class="feedback-text">{html.escape(error.message)}{details}</td>'
            "</tr>"
        )

    return (
        "<h1>Ошибки</h1>"
        '<p class="legend">Последние записи уровня ERROR из журнала приложения, '
        "до 500 штук.</p><table><thead><tr>"
        f"<th>Когда, {html.escape(datetime.now(zone).tzname() or '')}</th>"
        "<th>Где</th><th>Что</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )
