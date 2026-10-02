import hmac

from aiohttp import web

from app.core.config import Config
from app.core.identity import WEB, Identity

# Single-user deployment. Every web request resolves to this identity until
# per-user accounts exist.
DEFAULT_WEB_IDENTITY = Identity(kind=WEB, id="default")

AUTH_HEADER = "X-Platform-Auth"


def verify_access(request: web.Request) -> web.Response | None:
    """Reject requests that do not carry the shared secret.

    The web interface is exposed through a reverse proxy that terminates TLS
    and authenticates the user. This check is the second layer: it guarantees
    that only the proxy, which holds the secret, can reach the application.
    Without it, anything able to reach the port could issue commands.
    """
    if not Config.WEB_ACCESS_TOKEN:
        return None

    provided = request.headers.get(AUTH_HEADER, "")
    expected = Config.WEB_ACCESS_TOKEN

    if not hmac.compare_digest(provided, expected):
        return web.json_response({"error": "Unauthorized"}, status=401)

    return None


def resolve_identity(request: web.Request) -> Identity:
    """Resolve the caller from the request.

    This is the single place a request becomes a user identity, which makes it
    the seam for per-user accounts. Adding those later means replacing this
    body; no plugin or repository changes.
    """
    return DEFAULT_WEB_IDENTITY
