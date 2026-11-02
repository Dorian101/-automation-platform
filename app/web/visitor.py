"""What the visit log may say about a request without keeping who sent it.

Three things, each computed on the way in and none of them reversible: a hash
that groups one browser's visits within one day, a short description of the
browser, and whether the caller is a robot. The address and the full browser
string are used here and never written down.
"""

import hashlib
import re

from aiohttp import web

#: Long enough that two visitors on one day practically never share one,
#: short enough to read in a table.
FINGERPRINT_LENGTH = 12

#: Crawlers, link previews, monitors and scripts. Matched anywhere in the
#: browser string, case-insensitively. Link previews are here on purpose:
#: sending a link in a chat makes the messenger fetch it ("TelegramBot",
#: "WhatsApp/2…"), which is not somebody reading the page. Telegram's own
#: in-app browser says "Telegram-Android" without "bot" and stays a person.
_BOT = re.compile(
    r"bot\b|bot/|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|"
    r"vkshare|headless|lighthouse|pingdom|uptime|monitor|"
    r"curl|wget|python|httpx|aiohttp|go-http-client|java/|okhttp|axios|"
    r"node-fetch|libwww|scrapy",
    re.IGNORECASE,
)

# Order matters: an Edge or Yandex string also says Chrome and Safari, and a
# Chrome string also says Safari.
_BROWSERS = [
    ("YaBrowser", "Яндекс Браузер"),
    ("Edg", "Edge"),
    ("OPR", "Opera"),
    ("Firefox", "Firefox"),
    ("FxiOS", "Firefox"),
    ("CriOS", "Chrome"),
    ("Chrome", "Chrome"),
    ("Safari", "Safari"),
]

_SYSTEMS = [
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("Windows", "Windows"),
    ("Mac OS X", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
]

#: What a `?from=` tag may look like. Anything else is ignored rather than
#: stored: the tag is the owner's own label on a link, not free text.
_CAMPAIGN = re.compile(r"[a-z0-9_-]{1,32}")


def is_bot(agent: str) -> bool:
    """Whether the browser string belongs to a robot. No string at all is one."""
    return not agent.strip() or bool(_BOT.search(agent))


def describe(agent: str) -> str:
    """A short label for the browser string: "iPhone · Safari", or "—"."""
    system = next((name for mark, name in _SYSTEMS if mark in agent), "")
    browser = next((name for mark, name in _BROWSERS if mark in agent), "")

    return " · ".join(part for part in (system, browser) if part) or "—"


def fingerprint(salt: bytes, address: str, agent: str) -> str:
    """The same for one address and browser on one day's salt, and nothing
    that leads back to either."""
    digest = hashlib.sha256(salt + address.encode() + b"\n" + agent.encode())

    return digest.hexdigest()[:FINGERPRINT_LENGTH]


def campaign(request: web.Request) -> str | None:
    """The `?from=` tag of the link that brought the visitor, if it is one."""
    tag = request.query.get("from", "").strip().lower()

    return tag if _CAMPAIGN.fullmatch(tag) else None
