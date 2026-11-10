import html
import logging
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web
from psycopg.errors import UniqueViolation
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
)

from app.core.config import Config
from app.db.passkeys_repo import TooManyPasskeys
from app.db.sessions_repo import digest

from . import visitor as visit_details
from .auth import SESSION_COOKIE
from .common import form_value, render_card
from .csrf import CSRF_FIELD
from .documents import PUBLIC_BASE_URL as ORIGIN
from .ratelimit import client_ip
from .responses import redirect as _redirect
from .responses import token_rejected

logger = logging.getLogger(__name__)

PENDING_COOKIE = "platform_passkey"
CHALLENGE_SECONDS = 300
RP_NAME = "Платформа автоматизации"


def rp_id() -> str:
    return urlsplit(ORIGIN).hostname or ""


def _purge(store: dict) -> None:
    now = time.monotonic()
    for stale in [key for key, (expires, _) in store.items() if expires < now]:
        del store[stale]


def _take(store: dict, key: str) -> dict | None:
    _purge(store)
    found = store.pop(key, None)
    return found[1] if found else None


def _put(store: dict, key: str, **values) -> None:
    _purge(store)
    store[key] = (time.monotonic() + CHALLENGE_SECONDS, values)


def _descriptors(keys) -> list[PublicKeyCredentialDescriptor]:
    return [PublicKeyCredentialDescriptor(id=key.credential_id) for key in keys]


def passkeys_panel(keys, csrf: str, verified: bool, admin: bool) -> str:
    if not rp_id():
        return '<p class="hint">Passkeys на этом сервере не настроены.</p>'

    if admin and not verified:
        hint = "Админка открывается только после входа с passkey. " + (
            "Выйдите и войдите снова." if keys else "Добавьте ключ на этом устройстве."
        )
    elif keys:
        hint = "Вход по паролю подтверждается одним из этих ключей."
    else:
        hint = "Ключ на устройстве защитит вход, даже если пароль узнают."

    rows = (
        "".join(
            "<li>"
            f"{html.escape(key.label or 'Ключ')} · с {key.created_at:%d.%m.%Y}"
            '<form method="post" action="/account/passkeys/remove" class="inline">'
            f'<input type="hidden" name="{CSRF_FIELD}" value="{html.escape(csrf)}">'
            f'<input type="hidden" name="id" value="{key.id}">'
            '<button type="submit" class="secondary">Удалить</button>'
            "</form></li>"
            for key in keys
        )
        if verified
        else ""
    )
    listing = f'<ul class="devices">{rows}</ul>' if rows else ""
    add = (
        '<button type="button" id="passkey-add">Добавить passkey</button>'
        if verified or not keys
        else ""
    )

    return (
        f'<div id="passkeys" data-csrf="{html.escape(csrf)}">'
        f'<p class="hint" id="passkey-state">{hint}</p>{add}{listing}</div>'
        '<script src="/passkey.js" defer></script>'
    )


class PasskeyRoutes:
    def _passkeys_block(self, request: web.Request, user, csrf: str) -> str:
        return passkeys_panel(
            self._passkeys.for_user(user.id),
            csrf,
            request.get("passkey", False),
            request.get("account_admin", False),
        )

    async def _passkey_request(self, request: web.Request):
        user, payload, refused = await self._json_request(request)

        if refused is not None:
            return None, None, refused

        if not rp_id():
            return (
                None,
                None,
                web.json_response(
                    {"error": "Passkeys на этом сервере не настроены."}, status=503
                ),
            )

        if not request.get("passkey") and self._passkeys.has_any(user.id):
            return (
                None,
                None,
                web.json_response(
                    {"error": "Сначала войдите с уже добавленным passkey."}, status=403
                ),
            )

        return user, payload, None

    async def _passkey_options(self, request: web.Request) -> web.Response:
        user, _, refused = await self._passkey_request(request)

        if refused is not None:
            return refused

        options = generate_registration_options(
            rp_id=rp_id(),
            rp_name=RP_NAME,
            user_id=str(user.id).encode(),
            user_name=user.username,
            exclude_credentials=_descriptors(self._passkeys.for_user(user.id)),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
            ),
            timeout=CHALLENGE_SECONDS * 1000,
        )
        _put(
            self._enrolments,
            digest(request.cookies.get(SESSION_COOKIE, "")),
            user_id=user.id,
            challenge=options.challenge,
        )

        return web.Response(
            text=options_to_json(options), content_type="application/json"
        )

    async def _passkey_register(self, request: web.Request) -> web.Response:
        user, payload, refused = await self._passkey_request(request)

        if refused is not None:
            return refused

        token = request.cookies.get(SESSION_COOKIE, "")
        pending = _take(self._enrolments, digest(token))

        if pending is None or pending["user_id"] != user.id:
            return web.json_response({"error": "Начните заново."}, status=400)

        try:
            verified = verify_registration_response(
                credential=payload.get("credential"),
                expected_challenge=pending["challenge"],
                expected_rp_id=rp_id(),
                expected_origin=ORIGIN,
            )
        except Exception as error:
            logger.warning("Rejected a passkey for %r: %s", user.username, error)
            return web.json_response({"error": "Ключ не прошёл проверку."}, status=400)

        label = visit_details.describe(request.headers.get("User-Agent", ""))

        try:
            self._passkeys.add(
                user.id,
                verified.credential_id,
                verified.credential_public_key,
                verified.sign_count,
                label,
            )
        except TooManyPasskeys:
            return web.json_response({"error": "Слишком много ключей."}, status=409)
        except UniqueViolation:
            return web.json_response({"error": "Этот ключ уже добавлен."}, status=409)

        self._sessions.mark_passkey(token)
        self._account_changed(user)
        logger.info("Added a passkey for %r", user.username)

        return web.json_response({"ok": True})

    async def _passkey_remove(self, request: web.Request) -> web.Response:
        form = await request.post()
        user = self._current_user(request)

        if user is None:
            return _redirect("/login")

        if not self._csrf.accepts(
            request.cookies.get(SESSION_COOKIE, ""), form_value(form, CSRF_FIELD)
        ):
            return token_rejected(request, "/")

        try:
            passkey_id = int(form_value(form, "id"))
        except ValueError:
            return _redirect("/")

        if request.get("passkey") and self._passkeys.remove(user.id, passkey_id):
            self._account_changed(user)
            logger.info("Removed a passkey of %r", user.username)

        return _redirect("/")

    def _passkey_step(self, request: web.Request, user, keys) -> web.Response:
        if not rp_id():
            logger.warning(
                "Passkey sign-in for %r with no PUBLIC_BASE_URL", user.username
            )
            return self._render_login(
                request,
                error="Вход по passkey на этом сервере не настроен.",
                username=user.username,
                status=503,
            )

        options = generate_authentication_options(
            rp_id=rp_id(),
            allow_credentials=_descriptors(keys),
            timeout=CHALLENGE_SECONDS * 1000,
        )
        pending = secrets.token_urlsafe(32)
        _put(
            self._pending_logins,
            pending,
            user_id=user.id,
            challenge=options.challenge,
            agent=visit_details.describe(request.headers.get("User-Agent", "")),
        )

        response = render_card(
            "login_passkey.html",
            {"__OPTIONS__": html.escape(options_to_json(options))},
        )
        response.set_cookie(
            PENDING_COOKIE,
            pending,
            max_age=CHALLENGE_SECONDS,
            path="/login",
            httponly=True,
            samesite="strict",
            secure=Config.SESSION_COOKIE_SECURE,
        )

        return response

    async def _passkey_login(self, request: web.Request) -> web.Response:
        pending = _take(self._pending_logins, request.cookies.get(PENDING_COOKIE, ""))

        if pending is None:
            return web.json_response({"error": "Войдите заново."}, status=400)

        user_id = pending["user_id"]

        try:
            credential = (await request.json())["credential"]
            raw_id = credential["rawId"]
            found = self._passkeys.find(user_id, base64url_to_bytes(raw_id))
            if found is None:
                raise ValueError("unknown credential")
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=pending["challenge"],
                expected_rp_id=rp_id(),
                expected_origin=ORIGIN,
                credential_public_key=found.public_key,
                credential_current_sign_count=found.sign_count,
            )
        except Exception as error:
            logger.warning(
                "Rejected a passkey sign-in from %s: %s", client_ip(request), error
            )
            return web.json_response(
                {"error": "Ключ не подошёл. Войдите заново."}, status=401
            )

        user = self._users.get_by_id(user_id)

        if user is None or not user.is_active:
            return web.json_response({"error": "Войдите заново."}, status=401)

        self._passkeys.used(found.id, verified.new_sign_count)
        response = web.json_response({"location": "/"})
        response.del_cookie(PENDING_COOKIE, path="/login")

        return self._open_session(
            request, user, response, pending["agent"], passkey=True
        )
