"""Cross-site request forgery protection for the HTML forms.

SameSite=Lax already keeps the platform's own cookies off cross-site POSTs;
these tokens are the second, independent line, so a SameSite gap on some
browser or a page served from a sibling subdomain cannot forge a state change.

Anonymous forms (sign in, sign up) are protected by double submission: the
page issues a random token in a cookie and echoes the same value into the
form, and a submission is accepted only when the two match. Forms behind a
session (sign out, unlink) derive their token from the session token, which
only the signed-in browser holds, so nothing extra is stored and a value
captured from one session does not work in another.

The anonymous cookie is HttpOnly and SameSite=Lax: the page supplies the value
to echo back, and the browser withholds cookies from exactly the cross-site
requests a forged form would produce.
"""

import base64
import hmac
import logging
import secrets

CSRF_COOKIE = "platform_csrf"
CSRF_FIELD = "csrf"

#: An hour, not ten minutes. The value is a random 256-bit token that is only
#: ever compared against the copy the page shows, and the cookie holding it is
#: HttpOnly and SameSite=Lax. What the window really costs is patience: at ten
#: minutes a form that sat open long enough came back "stale" and the site
#: looked broken to the person trying to sign in to it.
#:
#: An hour covers filling in a username and a password, including a password
#: from a manager, without a reload in between.
CSRF_TTL_SECONDS = 3600

logger = logging.getLogger(__name__)

#: The answer the session-backed handlers return for a bad token. Public so
#: that tests assert against the same words.
FORGED_FORM_MESSAGE = "Форма устарела. Обновите страницу и попробуйте снова."


class CsrfSigner:
    """Derives per-session CSRF values from a secret that outlives the process.

    With `secret` the tokens a browser already holds stay valid across a
    restart, which is the point: a page that was open when a deploy went out
    keeps working instead of coming back "the form has expired" with nothing
    the reader did wrong.

    Without one, a random secret per process — the behaviour of a deployment
    that has not set `CSRF_SECRET`, and the reason the failure exists at all.
    The token is an HMAC over a value only the session's own browser knows, so
    the secret never travels; it only has to be the same one tomorrow.
    """

    def __init__(self, secret: str = "") -> None:
        if secret:
            self._secret = secret.encode("utf-8")
        else:
            self._secret = secrets.token_bytes(32)
            logger.warning(
                "CSRF_SECRET is not set: forms open across a restart will be "
                "refused. Set it in .env to a random value."
            )

    def session_token(self, session_token: str) -> str:
        """The CSRF value for a signed-in form.

        Derived from the session token, which is exactly what only the
        browser that owns the session holds. The value is stable for the life
        of the session, so a page left open does not go stale.
        """
        return _sign(self._secret, session_token)

    def accepts(self, session_token: str, submitted: str) -> bool:
        """Whether a submitted value matches this session's token."""
        if not submitted:
            return False

        return _equal(self.session_token(session_token), submitted)


def new_token() -> str:
    """A fresh random value for the double-submit cookie and its form field."""
    return secrets.token_urlsafe(32)


def is_probably_a_token(value: str) -> bool:
    """Whether a value is shaped like one of ours.

    `token_urlsafe(32)` is 43 characters of URL-safe base64. Used before a
    cookie is echoed back into a form: a value that was mangled in transit
    would otherwise be printed into the form too, and the two halves would
    agree on garbage and pass the check while proving nothing.

    Not a security property. The comparison below is still constant-time and
    still decides the outcome; this only avoids making a broken cookie into a
    broken form.
    """
    return len(value) == 43 and all(c.isalnum() or c in "-_" for c in value)


def has_valid_cookie(cookie: str, submitted: str) -> bool:
    """Whether the echoed form field matches the browser's cookie."""
    if not cookie or not submitted:
        return False

    return _equal(cookie, submitted)


def _sign(secret: bytes, value: str) -> str:
    digest = hmac.new(secret, value.encode("utf-8"), "sha256").digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _equal(left: str, right: str) -> bool:
    """Constant-time compare, on bytes so any submitted string is safe."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
