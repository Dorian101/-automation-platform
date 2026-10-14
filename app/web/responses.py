"""The two answers a web handler keeps needing.

Small, and shared rather than repeated: the administrative area and the main
web layer both send browsers elsewhere and both refuse a POST that carries no
token, and two copies of either is how they end up answering differently.
"""

from aiohttp import web

from .csrf import FORGED_FORM_MESSAGE


def redirect(location: str) -> web.Response:
    """Send the browser to another path.

    A plain response rather than `web.HTTPFound`: returning an HTTPException
    from a handler is deprecated in aiohttp, and raising one would mean
    setting a cookie on an exception object.
    """
    return web.Response(status=303, headers={"Location": location})


def form_forged() -> web.Response:
    """Answer a session-backed POST that did not carry a valid token.

    Plain text rather than a re-render: unlike the anonymous forms there is no
    form on this page to refresh, and reloading takes the browser back to a
    page where a fresh token waits.
    """
    return web.Response(
        text=FORGED_FORM_MESSAGE,
        status=403,
        content_type="text/plain",
    )
