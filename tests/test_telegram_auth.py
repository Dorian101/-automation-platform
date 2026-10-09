"""Verification of Login Widget payloads.

The signature is the part that decides whether a Telegram user really signed
this, and the two checks around it — age and the caller's nonce — decide
whether that signature is worth anything right now. All three are here.
"""

import hashlib
import hmac
import time

import pytest

from app.core.config import Config
from app.web.telegram_auth import TelegramAuthError, verify_login

BOT_TOKEN = "123456:ABC-DEF_token_for_tests"

CHAT_ID = 4242


def signed_payload(bot_token: str = BOT_TOKEN, **overrides) -> dict[str, str]:
    """Build a payload carrying a signature Telegram would have produced."""
    payload = {
        "id": str(CHAT_ID),
        "first_name": "Alexey",
        "username": "alexey",
        "auth_date": str(int(time.time())),
    }
    payload.update(overrides)

    check_string = "\n".join(
        f"{key}={payload[key]}" for key in sorted(payload) if key != "hash"
    )

    key = hashlib.sha256(bot_token.encode()).digest()
    payload["hash"] = hmac.new(
        key, check_string.encode(), hashlib.sha256
    ).hexdigest()

    return payload


class TestAcceptingARealPayload:
    def test_returns_the_user_behind_it(self):
        user = verify_login(signed_payload(), BOT_TOKEN)

        assert user.id == CHAT_ID
        assert user.username == "alexey"
        assert user.first_name == "Alexey"

    def test_optional_fields_may_be_absent(self):
        payload = signed_payload()
        del payload["username"]
        del payload["first_name"]
        payload.pop("hash")
        payload["hash"] = _resign(payload)

        user = verify_login(payload, BOT_TOKEN)

        # Telegram only sends these when the person set them, and a missing
        # username must not be read as a failed login.
        assert user.username == ""
        assert user.first_name == ""

    def test_a_group_chat_id_is_accepted(self):
        """Chat ids are negative for groups, and the column holds both."""
        payload = signed_payload(id="-1001234567890")

        assert verify_login(payload, BOT_TOKEN).id == -1001234567890


class TestSignature:
    def test_a_tampered_field_is_rejected(self):
        payload = signed_payload()
        payload["id"] = str(CHAT_ID + 1)

        with pytest.raises(TelegramAuthError):
            verify_login(payload, BOT_TOKEN)

    def test_a_tampered_field_is_rejected_even_when_re_signed(self):
        """The signature binds the whole payload, not just the id.

        Re-signing after changing the id would defeat any check that only
        looked at the id, which is the payload this verification exists to
        accept.
        """
        payload = signed_payload(id=str(CHAT_ID + 1))

        with pytest.raises(TelegramAuthError):
            verify_login(payload, "a-different-bot-token")

    def test_a_payload_from_another_bot_is_rejected(self):
        payload = signed_payload(bot_token="999:another-bot-token")

        with pytest.raises(TelegramAuthError):
            verify_login(payload, BOT_TOKEN)

    def test_an_unsigned_payload_is_rejected(self):
        payload = signed_payload()
        del payload["hash"]

        with pytest.raises(TelegramAuthError):
            verify_login(payload, BOT_TOKEN)

    def test_an_empty_signature_is_rejected(self):
        with pytest.raises(TelegramAuthError):
            verify_login({"id": "1", "hash": ""}, BOT_TOKEN)

    def test_field_order_does_not_matter(self):
        payload = signed_payload()

        shuffled = {key: payload[key] for key in reversed(list(payload))}

        assert verify_login(shuffled, BOT_TOKEN).id == CHAT_ID

    def test_the_signature_itself_is_not_part_of_what_is_signed(self):
        """`hash` cannot be an input to itself.

        Telegram signs every field except the signature. If the check string
        included it, no payload could ever verify, since the value is what is
        being computed.
        """
        assert verify_login(signed_payload(), BOT_TOKEN).id == CHAT_ID

    def test_verifying_against_an_empty_token_is_refused(self):
        """Without a token the key is known to nobody, including us."""
        payload = signed_payload(bot_token="")

        with pytest.raises(TelegramAuthError):
            verify_login(payload, "")


class TestAge:
    """A valid signature never stops being valid, so it carries a deadline."""

    def test_a_recent_payload_is_accepted(self):
        payload = signed_payload(auth_date=str(int(time.time()) - 60))

        assert verify_login(payload, BOT_TOKEN).id == CHAT_ID

    def test_an_old_payload_is_rejected(self):
        stale = int(time.time()) - Config.TELEGRAM_LOGIN_MAX_AGE_SECONDS - 60
        payload = signed_payload(auth_date=str(stale))

        with pytest.raises(TelegramAuthError):
            verify_login(payload, BOT_TOKEN)

    def test_a_payload_from_the_future_is_rejected(self):
        """Otherwise a far-future timestamp would stay fresh for ever."""
        ahead = int(time.time()) + Config.TELEGRAM_LOGIN_MAX_AGE_SECONDS + 60
        payload = signed_payload(auth_date=str(ahead))

        with pytest.raises(TelegramAuthError):
            verify_login(payload, BOT_TOKEN)

    def test_a_small_clock_skew_is_forgiven(self):
        """Clocks disagree by seconds, and a login is not worth failing over."""
        payload = signed_payload(auth_date=str(int(time.time()) + 5))

        assert verify_login(payload, BOT_TOKEN).id == CHAT_ID

    def test_a_missing_auth_date_is_rejected(self):
        payload = signed_payload()
        del payload["auth_date"]
        payload.pop("hash")
        payload["hash"] = _resign(payload)

        with pytest.raises(TelegramAuthError):
            verify_login(payload, BOT_TOKEN)

    def test_a_non_numeric_auth_date_is_rejected(self):
        payload = signed_payload(auth_date="yesterday")

        with pytest.raises(TelegramAuthError):
            verify_login(payload, BOT_TOKEN)


class TestId:
    def test_a_missing_id_is_rejected(self):
        payload = signed_payload()
        del payload["id"]
        payload.pop("hash")
        payload["hash"] = _resign(payload)

        with pytest.raises(TelegramAuthError):
            verify_login(payload, BOT_TOKEN)

    def test_a_non_numeric_id_is_rejected(self):
        payload = signed_payload(id="not-a-number")

        with pytest.raises(TelegramAuthError):
            verify_login(payload, BOT_TOKEN)


class TestErrorMessages:
    """Errors reach a browser, so they must not say which check failed.

    Naming the check that rejected a payload tells an attacker which one to
    work on, and none of these are worth helping with.
    """

    @pytest.mark.parametrize(
        "payload,token",
        [
            (signed_payload(), "wrong-token"),
            (signed_payload(bot_token=""), ""),
            (signed_payload(auth_date="0"), BOT_TOKEN),
            (signed_payload(auth_date="yesterday"), BOT_TOKEN),
            (signed_payload(id="not-a-number"), BOT_TOKEN),
        ],
    )
    def test_no_message_names_the_check_that_failed(self, payload, token):
        with pytest.raises(TelegramAuthError) as caught:
            verify_login(payload, token)

        message = str(caught.value).lower()

        assert "signature" not in message
        assert "token" not in message

    def test_an_unsigned_payload_is_rejected_like_a_wrong_one(self):
        """Same outcome either way, so a missing hash learns nothing."""
        unsigned = signed_payload()
        del unsigned["hash"]

        messages = set()

        # One payload with no signature at all, one whose signature is not the
        # one this bot would have produced.
        for payload, token in ((unsigned, BOT_TOKEN), (signed_payload(), "other")):
            with pytest.raises(TelegramAuthError) as caught:
                verify_login(payload, token)

            messages.add(str(caught.value))

        assert len(messages) == 1

    def test_an_expired_payload_says_so(self):
        """Being told it expired is not an attacker's shortcut: that payload
        can never be revived, so knowing why it failed helps nobody."""
        stale = int(time.time()) - Config.TELEGRAM_LOGIN_MAX_AGE_SECONDS - 60

        with pytest.raises(TelegramAuthError, match="expired"):
            verify_login(signed_payload(auth_date=str(stale)), BOT_TOKEN)


def _resign(payload: dict[str, str], bot_token: str = BOT_TOKEN) -> str:
    """Sign a payload the way Telegram does, for building test cases."""
    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(payload.items()) if key != "hash"
    )

    return hmac.new(
        hashlib.sha256(bot_token.encode()).digest(),
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()
