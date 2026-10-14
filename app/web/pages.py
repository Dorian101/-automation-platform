"""Painting a plugin's page from the description it returned.

Everything here is about turning a dict into HTML. The plugin supplies the
content — titles, fields, bars, a funnel, totals — and never any markup of its
own, which is what lets a plugin add a field without the web layer growing a
branch for it.

The same function paints the account's page and the demonstration's. That is
the whole claim the demonstration rests on: it is not a second front end, it
is these plugins with a different identity in front of them.

The values a plugin sends are treated as text throughout. Anything that lands
in the page goes through ``html.escape`` or a ``textContent`` assignment, so
a note or a category a person typed cannot become markup.
"""

import html
import json
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

SESSION_ACTION_URL = "/api/page/action"
DEMO_ACTION_URL = "/demo/api/action"

DEMO_PREFIX = "/demo"

# The links that exist on every page, in the order they are read: what can be
# used first, what can be read afterwards.
STATIC_LINKS = (("/about", "Обо мне"), ("/project", "Проект и метод"))

#: Spelled out rather than imported from `admin.py`: that module imports this
#: one for the menu styles, and a cycle would be a poor price for a constant.
ADMIN_PREFIX = "/admin"


def nav(current: str, pages: list[dict], guest: bool, admin: bool = False) -> str:
    """The one navigation, built from what the plugins actually declare.

    Five hand-written copies of this block is how the menu on the spending
    page lost its link to the applications page while the one on `/about`
    kept it: nothing but a copy to forget.

    A guest is offered only what they can reach. Their menu points the plugin
    pages at the demonstration's copies, because `/expenses` answers a visitor
    with a login form — the one place on the site that must not send a
    reader into a wall.

    The administrative link is the same arrangement one level down: it is in
    the menu for whoever holds the flag and absent for everybody else, and a
    guest is not an exception to be handled because a guest has no flag and
    so cannot be an administrator. The link is a convenience, not the check —
    the check is the middleware in `admin.py`, and hiding a link is not what
    keeps the page closed.

    Rendered into the page rather than fetched by script, so the links are
    there with JavaScript disabled and there is no state to keep in step.
    """
    items = []

    if guest:
        items.append((DEMO_PREFIX, "Демонстрация"))
    else:
        items.append(("/", "Платформа"))

    for page in pages:
        target = page["page"]
        items.append(
            (f"{DEMO_PREFIX}{target}" if guest else target, page["title"] or target)
        )

    if not guest:
        items.append((DEMO_PREFIX, "Демонстрация"))

    if admin:
        items.append((ADMIN_PREFIX, "Служебный раздел"))

    items.extend(STATIC_LINKS)

    links = []
    for href, title in items:
        # `aria-current` is the whole of "you are here": it is what a reader
        # looks at and what a screen reader announces, and it costs nothing.
        mark = ' aria-current="page"' if href == current else ""
        links.append(f'<a href="{html.escape(href)}"{mark}>{html.escape(title)}</a>')

    return (
        '<details class="menu">'
        '<summary>Меню</summary>'
        f'<nav class="menuitems">{"".join(links)}</nav>'
        "</details>"
    )


def menu_styles() -> str:
    """The menu's styles, read per request.

    The pages do not share a stylesheet — the console has its own and the
    documents another — so the menu is injected alongside the navigation
    rather than living in either. Defining it in the document stylesheet is
    what left it unstyled on the console: the markup arrived, the styling
    did not, and the `<details>` fell back to a row of underlined links.
    """
    return (TEMPLATES_DIR / "_menu_styles.html").read_text(encoding="utf-8")


def render_plugin_page(
    page_path: str,
    view: dict,
    nav_html: str,
    demo: bool = False,
) -> str:
    """Build the whole page for one plugin description.

    ``</`` is escaped inside the JSON so a value containing it cannot close the
    script element; the block is ``type="application/json"``, which the browser
    treats as data and never executes.
    """
    source = (TEMPLATES_DIR / "_plugin_page.html").read_text(encoding="utf-8")

    source = source.replace(
        "__TITLE__",
        html.escape(str(view.get("title", page_path))),
    )
    # The path, not the plugin name: it is what the action endpoint looks the
    # owning plugin up by, and sending the name there is what made every
    # button answer "Unknown page".
    source = source.replace("__PAGE_PATH__", html.escape(page_path))
    source = source.replace("__NAV__", nav_html)
    source = source.replace("__MENU_STYLES__", menu_styles())
    source = source.replace(
        "__ACTION_URL__",
        DEMO_ACTION_URL if demo else SESSION_ACTION_URL,
    )
    source = source.replace("__IS_DEMO__", "true" if demo else "false")
    source = source.replace(
        "__STYLES__",
        (TEMPLATES_DIR / "_page_styles.html").read_text(encoding="utf-8"),
    )
    source = source.replace(
        "__INITIAL_VIEW__",
        json.dumps(view, ensure_ascii=False).replace("</", "<\\/"),
    )

    return source


def render_demo_landing(links: list[dict], nav_html: str) -> str:
    """The front door to the demonstration.

    Lists the pages that exist rather than naming any, so a plugin added later
    appears here without this being edited.
    """
    source = (TEMPLATES_DIR / "_demo.html").read_text(encoding="utf-8")

    source = source.replace("__NAV__", nav_html)
    source = source.replace("__MENU_STYLES__", menu_styles())

    source = source.replace(
        "__STYLES__",
        (TEMPLATES_DIR / "_page_styles.html").read_text(encoding="utf-8"),
    )

    cards = []
    for link in links:
        cards.append(
            '<a class="card" href="{href}">'
            "<b>{title}</b>"
            '<span>{description}</span>'
            "</a>".format(
                href=html.escape("/demo" + link["page"]),
                title=html.escape(link.get("title") or link["page"]),
                description=html.escape(link.get("description", "")),
            )
        )

    source = source.replace("__CARDS__", "".join(cards))

    return source
