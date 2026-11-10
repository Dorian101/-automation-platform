import asyncio
import html
import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from time import perf_counter

from aiohttp import web

from app.core.commands import parse_command
from app.core.identity import WEB, Identity, telegram
from app.core.results import CommandError
from app.db.migrations import MigrationRunner
from app.db.settings_repo import DEFAULT_FEEDBACK_PER_HOUR

from . import i18n
from .admin import FEEDBACK_PATH
from .common import TEMPLATES_DIR
from .pages import (
    menu_styles,
    render_not_found,
)
from .ratelimit import client_ip

logger = logging.getLogger(__name__)


# The tab icon, beside this module rather than in `templates/`: it is an asset,
# not a page, and nothing renders it as markup.
FAVICON_SVG = Path(__file__).resolve().parent / "favicon.svg"


# The share preview image. It has to be a PNG: the messengers that render
# `og:image` do not render SVG, so the mark is drawn in `og-image.svg` and
# rasterised to `og-image.png`, which is what the tag points at. The SVG stays
# beside it so the look can be changed and re-rendered rather than redrawn from
# a binary.
OG_IMAGE_PNG = Path(__file__).resolve().parent / "og-image.png"


# Self-hosted fonts, beside the module like the icon and the preview image: an
# asset, not markup. The woff2 files and the `@font-face` rules live in
# `fonts/`, generated once and served without an external request.
FONTS_DIR = Path(__file__).resolve().parent / "fonts"


# The /about hero. A file rather than inline: the CSP allows scripts from this
# origin, and an inline one would need a nonce the page does not carry.
ABOUT_HERO_JS = Path(__file__).resolve().parent / "about_hero.js"


ABOUT_PAGE_JS = Path(__file__).resolve().parent / "about_page.js"


PROJECT_PAGE_JS = Path(__file__).resolve().parent / "project_page.js"


FEEDBACK_JS = Path(__file__).resolve().parent / "feedback.js"


APP_ASSETS = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


DOCUMENT_PATHS = ("/about", "/project")


LOCALIZED_DOCUMENTS = tuple(
    i18n.prefix(lang) + path for lang in i18n.LANGUAGES for path in DOCUMENT_PATHS
)


RELEASED_DOCUMENTS = tuple(
    i18n.prefix(lang) + path for lang in i18n.RELEASED for path in DOCUMENT_PATHS
)


SCRIPTS = {
    "/about-hero.js": ABOUT_HERO_JS,
    "/about-page.js": ABOUT_PAGE_JS,
    "/project-page.js": PROJECT_PAGE_JS,
    "/feedback.js": FEEDBACK_JS,
    "/push.js": Path(__file__).resolve().parent / "push.js",
    "/live-page.js": Path(__file__).resolve().parent / "live_page.js",
    "/sw.js": Path(__file__).resolve().parent / "sw.js",
}


# The live request on the project page. Public and it touches the database, so
# it is limited per address; a reader clicks it a few times, a script does not.
TRACE_ATTEMPTS = 30


TRACE_WINDOW_SECONDS = 60


# Transport, what runs, and whether it reaches the database.
TRACE_SCENARIOS = {
    "help": ("web", "/help", False),
    "notes": ("web", "/notes", True),
    "telegram": ("telegram", "/notes", True),
    "expenses": ("web", "/expenses", True),
    "reminder": ("web", "/reminders", True),
    "unknown": ("web", "/nosuchcommand", False),
}


# The guest book on the public pages. A person writes once or twice; more than
# a handful an hour from one address is somebody filling the owner's chat.
# The default; the administrator can change it on /admin/settings.
FEEDBACK_ATTEMPTS = DEFAULT_FEEDBACK_PER_HOUR


FEEDBACK_WINDOW_SECONDS = 3600


FEEDBACK_MAX_LENGTH = 2000


CAREER_START = date(2023, 7, 1)


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


# What a search engine is allowed to index. Everything else on this site either
# needs a session or is the console itself, and there is nothing in the
# console a reader would want in an index anyway.
INDEXABLE_PATHS = (*RELEASED_DOCUMENTS, "/demo")


def _experience(today: date, lang: str = i18n.DEFAULT) -> str:
    start = CAREER_START
    months = (today.year - start.year) * 12 + today.month - start.month + 1
    years, months = divmod(months, 12)
    words = i18n.strings(lang)
    parts = []

    if years:
        parts.append(f"{years} {i18n.plural(lang, years, words['experience.years'])}")

    if months:
        parts.append(
            f"{months} {i18n.plural(lang, months, words['experience.months'])}"
        )

    return " ".join(parts)


_PAGE_LANG = re.compile(r'<html lang="([a-z]+)"')


def theme_head(body: str = "") -> str:
    """The theme's script and light palette, for every page's head."""
    found = _PAGE_LANG.search(body)
    lang = found.group(1) if found else i18n.DEFAULT
    if lang not in i18n.LANGUAGES:
        lang = i18n.DEFAULT
    theme = (TEMPLATES_DIR / "_theme.html").read_text(encoding="utf-8")
    return theme + i18n.script_strings(lang)


def _render_page(
    template: str,
    nav_html: str,
    status: int = 200,
    replacements: Mapping[str, str] | None = None,
    path: str = "",
    lang: str = i18n.DEFAULT,
) -> web.Response:
    """Render one of the public document pages.

    A page that has to work without a session must not quietly grow a
    dependency on one, so the substitutions here are the page language's
    texts, the stylesheet and, where a page states a fact about the project,
    numbers read from the code. If a
    page starts needing anything per-visitor, that is the signal to reconsider
    whether it belongs in PUBLIC_PATHS at all.
    """
    source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    source = i18n.localize(source, lang)
    source = source.replace("__NAV__", nav_html)
    source = source.replace("__MENU_STYLES__", menu_styles())
    source = source.replace(
        "__PAGE_STYLES__",
        (TEMPLATES_DIR / "_page_styles.html").read_text(encoding="utf-8"),
    )

    for name, value in (replacements or {}).items():
        source = source.replace(name, value)

    if path:
        source = source.replace(
            "</head>",
            _share_tags(source, path)
            + _structured_data(source, path, lang)
            + _alternates(path)
            + "</head>",
        )

    return web.Response(text=source, content_type="text/html", status=status)


# Who the site is about, for search engines. The author of the code and the
# subject of /about are one person, so there is one description of them.
def _person(lang: str) -> dict:
    words = i18n.strings(lang)
    return {
        "@type": "Person",
        "name": words["person.name"],
        "jobTitle": words["person.job"],
        "email": "mailto:alexeyr101@gmail.com",
        "address": {
            "@type": "PostalAddress",
            "addressLocality": words["person.city"],
            "addressRegion": words["person.region"],
            "addressCountry": "RU",
        },
        "sameAs": ["https://t.me/alexeyr6"],
    }


def _source_code(lang: str) -> dict:
    return {
        "@type": "SoftwareSourceCode",
        "name": "Automation Platform",
        "codeRepository": "https://github.com/Dorian101/-automation-platform",
        "programmingLanguage": "Python",
        "author": _person(lang),
    }


STRUCTURED_DATA = {"/about": _person, "/project": _source_code}


def _alternates(path: str) -> str:
    lang, base = i18n.split(path)

    if not PUBLIC_BASE_URL or base not in DOCUMENT_PATHS or len(i18n.RELEASED) < 2:
        return ""

    links = [
        f'<link rel="alternate" hreflang="{other}" '
        f'href="{PUBLIC_BASE_URL}{i18n.prefix(other)}{base}">'
        for other in i18n.RELEASED
    ]
    links.append(
        f'<link rel="alternate" hreflang="x-default" href="{PUBLIC_BASE_URL}{base}">'
    )
    return "\n".join(links) + "\n"


_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)


_DESCRIPTION = re.compile(r'name="description" content="(.*?)"', re.DOTALL)


def _structured_data(source: str, path: str, lang: str = i18n.DEFAULT) -> str:
    """What a search engine reads about the page, as JSON-LD.

    A data block rather than a script, so the browser never runs it and the CSP
    has nothing to say. The description comes from the page, for the reason
    `_share_tags` gives, and the address needs a base URL the same way.
    """
    build = STRUCTURED_DATA.get(i18n.split(path)[1])

    if build is None:
        return ""

    data = {"@context": "https://schema.org", **build(lang)}
    description = _DESCRIPTION.search(source)

    if description is not None:
        data["description"] = html.unescape(description.group(1))

    if PUBLIC_BASE_URL:
        data["url"] = f"{PUBLIC_BASE_URL}{path}"

    body = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    return f'<script type="application/ld+json">{body}</script>'


def _share_tags(source: str, path: str) -> str:
    """What a messenger shows when the link is pasted.

    A résumé link is almost always passed on in a chat, and without these it
    arrives as a bare address: no title, no line of explanation, nothing that
    says what the page is before the reader decides to spend a second on it.

    Built from the page's own `<title>` and description rather than written
    beside them. A second copy of either is a second thing to keep right, and
    the one that goes wrong is the one nobody sees — it is not on the page.

    The image is the one part not read from the page: `/og-image.png` is the
    same mark as the favicon, drawn with the site name, and it needs a base
    URL the same way `og:url` does — a relative address means nothing to
    whoever fetches it. A deployment without `PUBLIC_BASE_URL` set gets no
    image and no url at all rather than a wrong one.
    """
    title = _TITLE.search(source)
    description = _DESCRIPTION.search(source)

    if title is None or description is None:
        return ""

    site_name = i18n.strings(i18n.split(path)[0])["site.name"]
    tags = [
        'property="og:type" content="website"',
        f'property="og:site_name" content="{site_name}"',
        f'property="og:title" content="{title.group(1)}"',
        f'property="og:description" content="{description.group(1)}"',
        'name="twitter:card" content="summary_large_image"',
    ]

    if PUBLIC_BASE_URL:
        tags.append(f'property="og:url" content="{PUBLIC_BASE_URL}{path}"')
        tags.append(f'property="og:image" content="{PUBLIC_BASE_URL}/og-image.png"')
        tags.append('property="og:image:width" content="1200"')
        tags.append('property="og:image:height" content="630"')

    return "".join(f"<meta {tag}>\n" for tag in tags)


class DocumentRoutes:
    async def _document(self, request: web.Request) -> web.Response:
        lang, base = i18n.split(request.path)

        if lang not in i18n.RELEASED and not self._is_admin(request):
            return web.Response(
                text=render_not_found(), content_type="text/html", status=404
            )

        replacements = (
            {"__EXPERIENCE__": _experience(date.today(), lang)}
            if base == "/about"
            else self._project_counts()
        )
        return _render_page(
            base.lstrip("/") + ".html",
            self._nav(request, lang),
            replacements=replacements,
            path=request.path,
            lang=lang,
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
            # The share preview image, for the same reason as the sitemap. A
            # messenger reads the page, and then fetches the image named by
            # og:image as a bot — the title and description render from the
            # first read, but the picture is a second request that `Disallow: /`
            # would refuse. The image is not page content and nothing here wants
            # it indexed; it only has to be fetchable.
            "Allow: /og-image.png",
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

    async def _og_image(self, request: web.Request) -> web.Response:
        """The share preview image, at the path `og:image` points to.

        Served as PNG because the messengers that fetch `og:image` rasterise
        rather than parse SVG. It is the same mark as the favicon, drawn large
        with the site name beside it; see `og-image.svg` for the source.
        """
        return web.Response(
            body=OG_IMAGE_PNG.read_bytes(),
            content_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def _app_asset(self, request: web.Request) -> web.Response:
        name, content_type = APP_ASSETS[request.path]
        return web.Response(
            body=(Path(__file__).resolve().parent / name).read_bytes(),
            content_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def _script(self, request: web.Request) -> web.Response:
        """A public page's script, served as a file for the CSP's sake."""
        return web.Response(
            body=SCRIPTS[request.path].read_bytes(),
            content_type="text/javascript",
            # Revalidated, because a page and a stale script of the day before
            # do not fit together.
            headers={"Cache-Control": "no-cache"},
        )

    async def _fonts(self, request: web.Request) -> web.Response:
        """A self-hosted font file or its stylesheet.

        The public pages reach for `/fonts/fonts.css` and `/fonts/*.woff2`.
        Both are served from the `fonts/` directory without an external
        request, so the typeface does not leak a visit to a font host and
        stays available on a host without outbound font traffic.

        The name is taken from the URL and matched against the directory, so a
        path that is not a file in there answers 404 rather than reading an
        arbitrary file.

        Cached for a year, because the files never change in place: a changed
        font or stylesheet is published under a new name. Edited in place, it
        would reach a returning reader a year late.
        """
        name = request.match_info.get("name", "")
        path = (FONTS_DIR / name).resolve()

        if FONTS_DIR.resolve() not in path.parents or not path.is_file():
            return web.Response(status=404)

        content_type = (
            "font/woff2"
            if name.endswith(".woff2")
            else "text/css"
        )

        return web.Response(
            body=path.read_bytes(),
            content_type=content_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    async def _trace(self, request: web.Request) -> web.Response:
        """Run one fixed scenario through the real code and report its path.

        Only the scenarios named in `TRACE_SCENARIOS` run, so a stranger cannot
        pass a command of their own. Each one reads and none writes. Telegram
        here means the identity a chat would have: no bot is involved.
        """
        if not self._rate.allow(
            f"trace|{client_ip(request)}", TRACE_ATTEMPTS, TRACE_WINDOW_SECONDS
        ):
            return web.json_response({"error": "Too many requests"}, status=429)

        scenario = TRACE_SCENARIOS.get(request.query.get("scenario", ""))

        if scenario is None:
            return web.json_response({"error": "Unknown scenario"}, status=400)

        transport, target, reads = scenario
        lang = request.query.get("lang", "")
        words = i18n.strings(lang if lang in i18n.LANGUAGES else i18n.DEFAULT)
        identity = (
            telegram(0) if transport == "telegram" else Identity(WEB, "guest:trace")
        )
        steps = [transport, "identity"]
        started = perf_counter()
        ok = True

        try:
            if target.startswith("/expenses"):
                plugin = self._manager.find_by_page(target)
                steps.append("page-route")
                await plugin.page_view(identity)
                reply = words["trace.page_built"].format(
                    name=plugin.description, code=plugin.name
                )
            else:
                command, args = parse_command(target)
                plugin = self._manager.find(command)
                steps.append("dispatcher")
                if plugin is not None:
                    steps.append("to-plugin")
                result = await self._manager.execute(command, args, identity)
                reply = result.text
        except CommandError as error:
            ok = False
            reply = str(error)
        except Exception:
            logger.exception("Trace failed")
            return web.json_response({"error": "Trace failed"}, status=503)

        if ok:
            steps.append(f"plugin-{plugin.name}")
            if reads:
                steps.append("postgres")
            # The one stage not executed: a stranger has no chat, and a public
            # button must not be able to message the owner's. The reply says so.
            if request.query["scenario"] == "reminder":
                steps.append("notify")
                reply = words["trace.reminder_route"]

        return web.json_response(
            {
                "steps": steps,
                "ok": ok,
                "reply": reply.partition("\n")[0],
                "ms": round((perf_counter() - started) * 1000, 1),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _feedback(self, request: web.Request) -> web.Response:
        """Keep a reader's note and pass it on to the administrators' chats.

        Public and it writes, so it is narrow: JSON only, which a form on
        another site cannot send without a preflight this app never answers;
        a few per address an hour; plain text of bounded length. A filled
        `trap` field is a bot that read the markup — it is told the note was
        taken and nothing is kept.
        """
        if request.content_type != "application/json":
            return web.json_response({"error": "JSON expected"}, status=415)

        if not self._rate.allow(
            f"feedback|{client_ip(request)}",
            self._settings.feedback_per_hour(),
            FEEDBACK_WINDOW_SECONDS,
        ):
            return web.json_response({"error": "Too many requests"}, status=429)

        try:
            payload = await request.json()
        except Exception:
            payload = None

        if not isinstance(payload, dict):
            return web.json_response({"error": "Bad request"}, status=400)

        text = payload.get("text")
        page = payload.get("page")

        if not isinstance(text, str) or not text.strip():
            return web.json_response({"error": "Напишите текст"}, status=400)

        text = text.strip()

        if len(text) > FEEDBACK_MAX_LENGTH:
            return web.json_response(
                {"error": f"Не больше {FEEDBACK_MAX_LENGTH} символов"}, status=400
            )

        if payload.get("trap"):
            return web.json_response({"ok": True})

        page = page if page in INDEXABLE_PATHS else ""

        try:
            self._feedback_log.add(text, page)
            self.admin_changed(FEEDBACK_PATH)
        except Exception:
            logger.exception("Feedback could not be saved")
            return web.json_response({"error": "Не удалось сохранить"}, status=503)

        # In the background: the note is saved, and a slow Telegram must not
        # keep the visitor waiting for an answer about it.
        where = f" ({page})" if page else ""
        task = asyncio.create_task(
            self._tell_admins(f"Отзыв с сайта{where}:\n\n{text}")
        )
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

        return web.json_response({"ok": True})

    async def _tell_admins(self, text: str) -> None:
        """Send to every active administrator's linked chat. The note is already
        saved, so a failure here costs the message, not the note."""
        if self._notifier is None:
            return

        for user in self._users.active_admins():
            try:
                await self._notifier.deliver(self._users.identity_for(user), text)
            except Exception:
                logger.exception("Feedback could not be sent to %s", user.username)
