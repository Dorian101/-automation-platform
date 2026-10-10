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
import secrets

CSRF_COOKIE = "platform_csrf"
CSRF_FIELD = "csrf"
CSRF_TTL_SECONDS = 600

#: The answer the session-backed handlers return for a bad token. Public so
#: that tests assert against the same words.
FORGED_FORM_MESSAGE = "This form has expired. Reload the page and try again."


class CsrfSigner:
    """Derives per-session CSRF values from a per-process secret.

    Restarting the process invalidates outstanding tokens, which is
    acceptable: every page load renders a fresh one.
    """

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

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
