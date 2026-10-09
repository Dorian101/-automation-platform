"""Verification of Telegram Login Widget payloads.

The widget redirects a browser back to a URL we choose, carrying the Telegram
user's id and a `hash` over the fields. Telegram signs with a key derived from
the bot token, so proving the signature is proving Telegram vouched for this
user — nothing here is asked of the person operating the browser.

Two things are checked beyond the signature, and both matter more than the
signature:

* `auth_date` is recent. A valid payload stays valid forever, so one captured
  from a proxy log or a referrer header could be replayed at any later time.
* The payload is bound to this browser by a nonce the caller holds. Without it,
  anyone able to obtain their own signed payload could ask someone else's
  browser to submit it, and that person's account would be linked to the
  attacker's Telegram. The signature proves a real Telegram user, not that the
  person at the keyboard is that user.

The nonce lives in `app/web/server.py`, which owns the session and the cookies.
"""

import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.config import Config


class TelegramAuthError(ValueError):
    """Raised when a payload cannot be trusted.

    The message never repeats what was wrong in detail: it goes back to a
    browser, and telling an attacker which of the checks failed tells them
    which one to attack next.
    """


@dataclass(frozen=True)
class TelegramUser:
    """The Telegram user behind a verified payload."""

    id: int
    username: str
    first_name: str
    auth_date: int


def verify_login(payload: Mapping[str, str], bot_token: str) -> TelegramUser:
    """Return the user behind a widget payload, or raise.

    Raises:
        TelegramAuthError: the payload is unsigned, wrongly signed, or too old.
    """
    if not bot_token:
        # Verifying against an empty key would still produce a hash, and
        # anything the caller could invent would match it.
        raise TelegramAuthError("could not verify this Telegram sign-in")

    received = payload.get("hash", "")

    if not received:
        raise TelegramAuthError("could not verify this Telegram sign-in")

    expected = _signature(_check_string(payload), bot_token)

    if not hmac.compare_digest(expected, received):
        # Deliberately vague. The exact reason is useful in a server log and
        # useful to an attacker probing which check to work on; this is
        # reported to whoever made the request.
        raise TelegramAuthError("could not verify this Telegram sign-in")

    # Age is only checked once the signature holds: an unsigned payload is
    # rejected for a better reason than an old one.
    auth_date = _auth_date(payload)

    if not _fresh(auth_date, Config.TELEGRAM_LOGIN_MAX_AGE_SECONDS):
        raise TelegramAuthError("this Telegram sign-in has expired")

    return TelegramUser(
        id=_telegram_id(payload),
        username=payload.get("username", ""),
        first_name=payload.get("first_name", ""),
        auth_date=auth_date,
    )


def _check_string(payload: Mapping[str, str]) -> str:
    """Build the string Telegram signed.

    Every field except the signature, sorted by name, one per line. Sorted
    because Telegram builds it the same way, and excluding `hash` because the
    signature cannot be an input to itself.
    """
    fields = [
        f"{key}={value}" for key, value in sorted(payload.items()) if key != "hash"
    ]

    return "\n".join(fields)


def _signature(check_string: str, bot_token: str) -> str:
    """Sign the check string the way Telegram does.

    The key is the SHA-256 of the bot token rather than the token itself,
    which is what the protocol specifies.
    """
    key = hashlib.sha256(bot_token.encode("utf-8")).digest()

    return hmac.new(key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _auth_date(payload: Mapping[str, str]) -> int:
    raw = payload.get("auth_date", "")

    try:
        return int(raw)
    except ValueError:
        raise TelegramAuthError("could not verify this Telegram sign-in") from None


def _telegram_id(payload: Mapping[str, str]) -> int:
    raw = payload.get("id", "")

    try:
        return int(raw)
    except ValueError:
        raise TelegramAuthError("could not verify this Telegram sign-in") from None


def _fresh(auth_date: int, max_age_seconds: int) -> bool:
    """Whether the payload was signed recently enough to be accepted."""
    # A payload from the future would pass the age check for ever, since the
    # same timestamp is always going to be in the past. Bounded, not rejected:
    # clocks disagree by seconds and a login is not worth failing over that.
    return -max_age_seconds <= time.time() - auth_date <= max_age_seconds
