import hmac

from aiohttp import web

from app.core.config import Config
from app.core.identity import Identity
from app.db.sessions_repo import SessionsRepository
from app.db.users_repo import UsersRepository

AUTH_HEADER = "X-Platform-Auth"

SESSION_COOKIE = "platform_session"

DAY_SECONDS = 86400


def verify_access(request: web.Request) -> web.Response | None:
    """Reject requests that do not carry the shared secret.

    The web interface is exposed through a reverse proxy that terminates TLS.
    This check guarantees that only the proxy, which holds the secret, can reach
    the application. Without it, anything able to reach the port could issue
    commands.

    It is a transport check and not an account check: it proves the request
    travelled through the proxy, not who the user is. Identifying the user is
    the job of resolve_identity below.
    """
    if not Config.WEB_ACCESS_TOKEN:
        return None

    provided = request.headers.get(AUTH_HEADER, "")
    expected = Config.WEB_ACCESS_TOKEN

    if not hmac.compare_digest(provided, expected):
        return web.json_response({"error": "Unauthorized"}, status=401)

    return None


def resolve_identity(
    request: web.Request,
    users: UsersRepository,
    sessions: SessionsRepository,
) -> Identity | None:
    """Return the identity behind the session cookie, if it is valid.

    This is the single place a request becomes a user identity, which is why
    per-user accounts needed no change in any plugin or repository: they all
    receive the identity that comes out of here.
    """
    token = request.cookies.get(SESSION_COOKIE, "")

    if not token:
        return None

    user_id = sessions.get_user_id(token)

    if user_id is None:
        return None

    user = users.get_by_id(user_id)

    if user is None or not user.is_active:
        return None

    return users.identity_for(user)


def set_session_cookie(response: web.Response, token: str) -> None:
    """Attach a fresh session to a response.

    HttpOnly keeps the token away from scripts, SameSite=Lax blocks cross-site
    submissions of the login and logout forms, and Secure stops the cookie from
    ever travelling in clear text.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=Config.SESSION_TTL_DAYS * DAY_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=Config.SESSION_COOKIE_SECURE,
    )


def clear_session_cookie(response: web.Response) -> None:
    response.del_cookie(SESSION_COOKIE, path="/")


def signup_enabled() -> bool:
    """Whether this deployment has a sign-up page at all.

    Stripped, so a variable holding only whitespace counts as unset: the gate
    and `signup_allowed()` have to agree, or the page would render and then
    reject every code typed into it.
    """
    return bool(Config.SIGNUP_INVITE_CODE.strip())


def signup_allowed(code: str) -> bool:
    """Decide whether an invite code may open a new account.

    An unset code must never let anyone through, not even an empty one: that
    is the default for every deployment that never thought about sign-up, and
    an accidental open door would be the worst possible reading of a missing
    variable. `compare_digest` reports two empty strings as equal, so the
    empty check has to come first.

    Both sides are stripped, because a code pasted with a stray space or
    newline is the likeliest mistake a person can make here, and rejecting it
    as "invalid invite code" would send them looking for the wrong problem.
    """
    expected = Config.SIGNUP_INVITE_CODE.strip()

    if not expected:
        return False

    return hmac.compare_digest(code.strip(), expected)
