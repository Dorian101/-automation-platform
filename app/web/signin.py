import html
import logging

from aiohttp import web
from psycopg.errors import UniqueViolation

from app.core.config import Config
from app.db.sessions_repo import digest
from app.db.visits_repo import SIGNIN, SIGNUP

from . import visitor as visit_details
from .admin import USERS_PATH
from .auth import (
    SESSION_COOKIE,
    clear_session_cookie,
    set_session_cookie,
)
from .common import error_block, form_value, leaves_no_trace, rate_key, render_card
from .csrf import (
    CSRF_COOKIE,
    CSRF_FIELD,
    CSRF_TTL_SECONDS,
    FORGED_FORM_MESSAGE,
    has_valid_cookie,
    is_probably_a_token,
    new_token,
)
from .ratelimit import client_ip
from .responses import redirect as _redirect
from .responses import token_rejected

logger = logging.getLogger(__name__)


DAY_SECONDS = 86400


def _form_token(request: web.Request) -> str:
    """The token to print into a form and to hand the browser as a cookie.

    Reuses whatever the browser already holds rather than minting a new one.
    Minting on every page load meant that two open tabs disagreed: the first
    tab was given a cookie and a form field, the second tab overwrote the
    cookie, and the browser then sent the *new* cookie with the *old* form.
    Double submission is meant to be the pair that matches, so any page load
    that changes one half without the other breaks it — and it broke in a way
    that looked like the site was refusing to let the owner in.

    A value that fails `is_probably_a_token` is replaced, so a cookie that
    was mangled in transit does not become the form's token as well.
    """
    existing = request.cookies.get(CSRF_COOKIE, "")

    if is_probably_a_token(existing):
        return existing

    return new_token()


def _set_csrf_cookie(response: web.Response, token: str) -> web.Response:
    """Tie the double-submit token to the browser that was handed the form.

    HttpOnly and SameSite=Lax, like the session cookie itself: the value is
    not for script to read, the page supplies it to echo back, and same-site
    is enough because the whole point is that a *cross*-site form does not get
    to carry cookies.

    Secure follows the session cookie rather than being decided here, and the
    flag is the right one to follow: this cookie only matters to a form that
    ends in a session, so a deployment with `SESSION_COOKIE_SECURE` on is
    https-only by definition — a browser will not accept a Secure cookie over
    http, which means the session could not be established there whatever this
    cookie did.

    That is also why the demonstration's guest cookie stays insecure, which is
    the one thing this change does not do: that cookie carries a session with
    it nowhere, being the only part of the site that needs no account, so
    marking it Secure would break the demonstration over http and buy nothing.
    """
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=CSRF_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=Config.SESSION_COOKIE_SECURE,
    )

    return response


def _signup_link(open_: bool) -> str:
    """Offer registration exactly where it works.

    With registration closed there is no sign-up page either — it answers 404 —
    so showing the link would send people to a dead end.
    """
    if not open_:
        return ""

    return '<p class="alt">Нет аккаунта? <a href="/signup">Регистрация</a></p>'


class SignInRoutes:
    async def _login_form(self, request: web.Request) -> web.Response:
        if self._identity(request) is not None:
            return _redirect("/")

        return self._render_login(request)

    async def _login_submit(self, request: web.Request) -> web.Response:
        form = await request.post()

        username = form_value(form, "username")
        password = form_value(form, "password")

        if not has_valid_cookie(
            request.cookies.get(CSRF_COOKIE, ""),
            form_value(form, CSRF_FIELD),
        ):
            # Rejected before any scrypt work: a forged submission is not a
            # real login attempt, and should not cost one.
            logger.warning(
                "Rejected a login without a valid CSRF token from %s",
                client_ip(request),
            )
            return self._render_login(request,
                error=FORGED_FORM_MESSAGE,
                username=username,
                status=400,
            )

        # The address window is checked first and its key carries no username.
        # Order is the point: this is the window that stops a caller producing a
        # fresh key per name tried, so it has to run before the per-user window
        # that creates those keys.
        if not self._rate.allow(
            rate_key(request, "login-address"),
            Config.LOGIN_ATTEMPTS_PER_ADDRESS_PER_WINDOW,
            Config.RATE_LIMIT_WINDOW_SECONDS,
        ):
            logger.warning(
                "Login rate limit reached for the address %s",
                client_ip(request),
            )
            return self._render_login(request,
                error="Слишком много попыток. Попробуйте позже.",
                username=username,
                status=429,
            )

        if not self._rate.allow(
            rate_key(request, "login", username),
            Config.LOGIN_ATTEMPTS_PER_WINDOW,
            Config.RATE_LIMIT_WINDOW_SECONDS,
        ):
            logger.warning(
                "Login rate limit reached for %r from %s",
                username,
                client_ip(request),
            )
            return self._render_login(request,
                error="Слишком много попыток. Попробуйте позже.",
                username=username,
                status=429,
            )

        user = self._users.authenticate(username, password)

        if user is None:
            # The message does not say which half was wrong: an unknown
            # username and a wrong password must look the same from outside.
            # The username is written with %r because it is caller-controlled
            # input and must not be able to forge a log line.
            logger.warning(
                "Failed login attempt for %r from %s", username, client_ip(request)
            )
            return self._render_login(request,
                error="Неверное имя пользователя или пароль.",
                username=username,
                status=401,
            )

        # A correct password after a run of typos reloads the allowance rather
        # than starting the next window from the typos' count.
        self._rate.clear(rate_key(request, "login", username))

        token = self._sessions.create(
            user.id,
            Config.SESSION_TTL_DAYS * DAY_SECONDS,
            agent=visit_details.describe(request.headers.get("User-Agent", "")),
        )

        response = _redirect("/")
        set_session_cookie(response, token)
        # The anonymous token has done its job; the session-backed forms carry
        # their own token derived from the session.
        response.del_cookie(CSRF_COOKIE, path="/")

        logger.info("Signed in %r from %s", user.username, client_ip(request))

        if not leaves_no_trace(request):
            self._visits.record(
                SIGNIN,
                path="/login",
                identity=str(self._users.identity_for(user)),
                **self._details(request),
            )
            self._visit_recorded()
        self.admin_changed(USERS_PATH)

        return response

    async def _logout(self, request: web.Request) -> web.Response:
        form = await request.post()

        token = request.cookies.get(SESSION_COOKIE, "")

        if not token:
            # No session to end and nothing that was asked of the cookie; the
            # repeated logout just bounces. There is no state change here, so
            # no CSRF check stands between a guest and a redirect.
            response = _redirect("/login")
            clear_session_cookie(response)
            return response

        if not self._csrf.accepts(token, form_value(form, CSRF_FIELD)):
            return token_rejected(request, "/login")

        identity = self._identity(request)

        self._sessions.delete(token)
        self._push.forget_session(digest(token))
        self.admin_changed(USERS_PATH)
        if identity is not None:
            self._live.changed(identity, "/")

        logger.info("Signed out %r", identity.id if identity else "unknown session")

        response = _redirect("/login")
        clear_session_cookie(response)
        return response

    async def _signup_form(self, request: web.Request) -> web.Response:
        # 404 rather than 403: with registration closed this deployment has no
        # sign-up page, and the path should behave like any other that does not
        # exist.
        if not self._settings.signup_open():
            return web.Response(status=404)

        if self._identity(request) is not None:
            return _redirect("/")

        return self._render_signup(request)

    async def _signup_submit(self, request: web.Request) -> web.Response:
        if not self._settings.signup_open():
            return web.Response(status=404)

        # Same as the GET: an existing session must not be quietly swapped for
        # the account someone is about to create. Signing out first is the
        # one step that makes that swap deliberate.
        if self._identity(request) is not None:
            return _redirect("/")

        form = await request.post()

        username = form_value(form, "username")
        password = form_value(form, "password")

        if not has_valid_cookie(
            request.cookies.get(CSRF_COOKIE, ""),
            form_value(form, CSRF_FIELD),
        ):
            logger.warning(
                "Rejected a sign-up without a valid CSRF token from %s",
                client_ip(request),
            )
            return self._render_signup(request,
                error=FORGED_FORM_MESSAGE,
                username=username,
                status=400,
            )

        if not self._rate.allow(
            rate_key(request, "signup"),
            Config.SIGNUP_ATTEMPTS_PER_WINDOW,
            Config.SIGNUP_WINDOW_SECONDS,
        ):
            logger.warning("Sign-up rate limit reached from %s", client_ip(request))
            return self._render_signup(request,
                error="Слишком много попыток. Попробуйте позже.",
                username=username,
                status=429,
            )

        try:
            user = self._users.create(username, password)
        except ValueError as error:
            # PasswordError is a ValueError, so a short password and an empty
            # username both arrive here carrying a message written for the
            # person filling in the form.
            return self._render_signup(request,
                error=str(error),
                username=username,
                status=400,
            )
        except UniqueViolation:
            return self._render_signup(request,
                error="Это имя уже занято.",
                username=username,
                status=409,
            )

        # Straight into a session: making someone sign in again immediately
        # after registering would only be an extra round trip for a form they
        # have just proved they can fill in.
        token = self._sessions.create(
            user.id,
            Config.SESSION_TTL_DAYS * DAY_SECONDS,
            agent=visit_details.describe(request.headers.get("User-Agent", "")),
        )

        response = _redirect("/")
        set_session_cookie(response, token)
        response.del_cookie(CSRF_COOKIE, path="/")

        logger.info(
            "Account created for %r from %s", user.username, client_ip(request)
        )

        if not leaves_no_trace(request):
            self._visits.record(
                SIGNUP,
                path="/signup",
                identity=str(self._users.identity_for(user)),
                **self._details(request),
            )
            self._visit_recorded()
        self.admin_changed(USERS_PATH)

        return response

    def _render_login(
        self,
        request: web.Request,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        token = _form_token(request)

        response = render_card(
            "login.html",
            {
                "__ERROR_BLOCK__": error_block(error),
                "__USERNAME__": html.escape(username),
                "__SIGNUP_BLOCK__": _signup_link(self._settings.signup_open()),
                "__CSRF__": token,
            },
            status=status,
        )

        return _set_csrf_cookie(response, token)

    def _render_signup(
        self,
        request: web.Request,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        token = _form_token(request)

        response = render_card(
            "signup.html",
            {
                "__ERROR_BLOCK__": error_block(error),
                "__USERNAME__": html.escape(username),
                "__CSRF__": token,
            },
            status=status,
        )

        return _set_csrf_cookie(response, token)
