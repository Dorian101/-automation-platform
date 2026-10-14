"""The two answers a web handler keeps needing.

Small, and shared rather than repeated: the administrative area and the main
web layer both send browsers elsewhere, and both have to say something when a
POST arrives without a valid token.
"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


def redirect(location: str) -> web.Response:
    """Send the browser to another path.

    A plain response rather than `web.HTTPFound`: returning an HTTPException
    from a handler is deprecated in aiohttp, and raising one would mean
    setting a cookie on an exception object.
    """
    return web.Response(status=303, headers={"Location": location})


def token_rejected(request: web.Request, location: str) -> web.Response:
    """A POST arrived without a valid CSRF token. Change nothing, and send the
    browser somewhere it can get a fresh one.

    A bare `403` in plain text was the wrong answer, and the reason took a
    while to find. A token belongs to the session it was drawn for, so it
    stops matching the moment that session ends — which happens on its own
    whenever somebody signs out in one tab and signs back in, or signs in as
    somebody else. Nothing was forged, the reader had done nothing wrong, and
    the page they got was a line of text on a black background with no way out
    of it and nothing to say what happened.

    Nothing is lost by redirecting: a refused token did not perform the
    action, whether the answer was 403 or 303, and a request author learns no
    more from a redirect than from a refusal. What the refusal *was* good for
    — leaving a trace — is kept in the log, where a reader will not mistake it
    for their own mistake.
    """
    logger.warning(
        "Refused a %s to %s without a valid token from %s",
        request.method,
        request.path,
        request.remote,
    )

    return redirect(location)
