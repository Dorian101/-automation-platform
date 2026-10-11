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


def render_plugin_page(
    page_path: str,
    view: dict,
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


def render_demo_landing(links: list[dict]) -> str:
    """The front door to the demonstration.

    Lists the pages that exist rather than naming any, so a plugin added later
    appears here without this being edited.
    """
    source = (TEMPLATES_DIR / "_demo.html").read_text(encoding="utf-8")

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
