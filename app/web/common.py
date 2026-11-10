import hmac
import html
from collections.abc import Mapping

from aiohttp import web

from app.core.config import Config

from .pages import TEMPLATES_DIR
from .ratelimit import client_ip

# What a request carries to say it is the owner's own working rather than a
# visit. Named beside the proxy's X-Platform-Auth because it is the same kind
# of thing — something only the holder of the value knows — and different in
# what it is for: the auth header is the door, this one is the log.
PROBE_HEADER = "X-Platform-Probe"


#: Where a template asks for the nonce. Substituted into the body and into the
#: header by one place, so a template never has to be handed the value to use
#: it — which is what keeps this to one edit rather than one per render path.
CSP_NONCE_PLACEHOLDER = "__CSP_NONCE__"


def rate_key(request: web.Request, action: str, name: str = "") -> str:
    """Key an attempt, binding it to the address that made it.

    For login the username rides along too, so one person's typos do not lock
    out a whole network behind the proxy. Sign-ups are keyed by address alone.
    """
    return f"{action}|{client_ip(request)}|{name.strip().lower()}"


def leaves_no_trace(request: web.Request) -> bool:
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


def render_card(
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


def error_block(message: str) -> str:
    if not message:
        return ""

    return f'<p class="error" role="alert">{html.escape(message)}</p>'


def form_value(form: Mapping[str, object], name: str) -> str:
    """Read a text field, rejecting anything that is not a plain string.

    A file upload arrives as a FileField, and str() on it would quietly turn
    the request into a bogus login attempt instead of a clean rejection.
    """
    value = form.get(name, "")
    return value if isinstance(value, str) else ""
