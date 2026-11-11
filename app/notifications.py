import asyncio
import json
import logging
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import requests
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pywebpush import WebPushException, webpush

from app.core.config import Config
from app.core.identity import TELEGRAM, WEB, Identity

logger = logging.getLogger("platform")


class NotificationChannel(Protocol):
    """Delivers a message to a user over one specific transport.

    ``deliver`` returns whether the message actually left the app. A channel
    that logs and drops — :class:`WebChannel` today — returns False, so a
    caller can tell "handled" from "delivered".
    """

    kind: str

    async def deliver(
        self, target: Identity, text: str, url: str = "/", buttons=()
    ) -> bool: ...


class TelegramChannel:
    kind = "telegram"

    def __init__(self, bot):
        self._bot = bot

    async def deliver(
        self, target: Identity, text: str, url: str = "/", buttons=()
    ) -> bool:
        if not buttons:
            await self._bot.send_message(chat_id=int(target.id), text=text)
            return True

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=label, callback_data=data)
                    for label, data in buttons
                ]
            ]
        )
        await self._bot.send_message(
            chat_id=int(target.id), text=text, reply_markup=markup
        )

        return True


class WebChannel:
    """Placeholder channel for users who arrived through the web interface.

    Web users have no inbox yet, so delivery is logged instead of sent. The
    signature matches :class:`NotificationChannel`, so giving the web interface
    a real inbox is a matter of replacing this class.

    Returns False rather than raising: nothing was delivered, but the caller
    has a message it may still handle on its own. The text is deliberately
    left out of the log: it is the user's own content — a reminder, a note —
    and it has no business in the journal.
    """

    kind = WEB

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def deliver(
        self, target: Identity, text: str, url: str = "/", buttons=()
    ) -> bool:
        self.logger.info(
            "Dropping web notification for %s (%d chars, text withheld)",
            target,
            len(text),
        )

        return False


PUSH_TITLE = "Платформа автоматизации"
PUSH_TEXT_LIMIT = 1000
PUSH_TIMEOUT_SECONDS = 10
PUSH_GONE = (404, 410)
RELEASE_INTERVAL_SECONDS = 60
HELD_GIVE_UP = timedelta(days=1)


def _session():
    session = requests.Session()
    session.trust_env = False
    session.max_redirects = 0
    return session


class WebPushChannel:
    kind = WEB

    def __init__(self, push, private_key: str, subject: str, send=webpush):
        self.logger = logging.getLogger(__name__)
        self._push = push
        self._key = private_key
        self._subject = subject
        self._send = send
        self.on_forgotten = None

    def reaches(self, target: Identity) -> bool:
        return bool(self._key and self._subject and self._push.subscriptions(target))

    async def deliver(
        self, target: Identity, text: str, url: str = "/", buttons=()
    ) -> bool:
        if not self._key or not self._subject:
            return False

        payload = json.dumps(
            {"title": PUSH_TITLE, "body": text[:PUSH_TEXT_LIMIT], "url": url},
            ensure_ascii=False,
        )
        delivered = False
        forgot = False

        for subscription in self._push.subscriptions(target):
            session = _session()
            try:
                await asyncio.to_thread(
                    self._send,
                    subscription_info=subscription,
                    data=payload,
                    vapid_private_key=self._key,
                    vapid_claims={"sub": self._subject},
                    timeout=PUSH_TIMEOUT_SECONDS,
                    ttl=86400,
                    headers={"Urgency": "high"},
                    requests_session=session,
                )
            except WebPushException as error:
                status = getattr(error.response, "status_code", None)
                if status in PUSH_GONE:
                    self._push.forget(subscription["endpoint"])
                    forgot = True
                else:
                    self.logger.warning("Push to %s failed with %s", target, status)
                continue
            except Exception:
                self.logger.exception("Push to %s failed", target)
                continue
            finally:
                session.close()

            delivered = True

        if forgot and self.on_forgotten is not None:
            self.on_forgotten(target)

        return delivered


class Notifier:
    """Routes a message to the channel matching the target's transport.

    A resolver can be supplied to answer "where should this reach, given who
    asked". The answer is an identity, and it may name a different transport
    than the one the request arrived on — a web account paired with a Telegram
    chat is delivered over Telegram, which is the whole point of pairing them.
    A resolver that raises means "this person has nowhere to be reached", and
    the message is dropped with the reason logged; that is not the same as a
    channel being missing, and the two are reported differently on purpose.
    """

    def __init__(
        self,
        channels: list[NotificationChannel],
        resolver=None,
        quiet=None,
    ):
        self.logger = logging.getLogger(__name__)
        self._channels = {channel.kind: channel for channel in channels}
        self._resolver = resolver
        self._quiet = quiet

    def kinds(self) -> list[str]:
        return list(self._channels)

    async def deliver(
        self,
        target: Identity,
        text: str,
        url: str = "/",
        buttons=(),
        urgent: bool = False,
    ) -> bool:
        """Deliver, reporting whether the message actually went anywhere.

        The caller needs the difference between "delivered" and "handled but
        not delivered": a message nobody could receive must not be retried for
        ever, and only the caller knows whether giving up is allowed.
        """
        if (
            not urgent
            and self._quiet is not None
            and self._quiet.hold(target, text, url, buttons)
        ):
            return True

        return await self._send(target, text, url, buttons)

    async def _send(self, target: Identity, text: str, url: str, buttons) -> bool:
        destination = self._destination(target)

        if destination is None:
            return False

        channel = self._channels.get(destination.kind)

        if channel is None:
            self.logger.warning(
                "No channel for transport %r, wanted for %s",
                destination.kind,
                target,
            )
            return False

        return bool(
            await channel.deliver(destination, text, url=url, buttons=buttons)
        )

    async def release_held(self) -> None:
        if self._quiet is None:
            return

        for held_id, target, body, stale in self._quiet.releasable():
            try:
                await self._send(
                    target,
                    body["text"],
                    body.get("url", "/"),
                    [tuple(button) for button in body.get("buttons", [])],
                )
            except Exception:
                if not stale:
                    continue
                self.logger.exception("Dropping held notification %s", held_id)
            self._quiet.release(held_id)

    async def run(self) -> None:
        while True:
            try:
                await self.release_held()
            except Exception:
                self.logger.exception("Releasing held notifications failed")
            await asyncio.sleep(RELEASE_INTERVAL_SECONDS)

    def reachable(self, target: Identity) -> bool:
        destination = self._destination(target)

        if destination is None:
            return False

        channel = self._channels.get(destination.kind)

        if channel is None:
            return False

        reaches = getattr(channel, "reaches", None)

        return reaches(destination) if reaches else destination.kind == TELEGRAM

    def destination(self, target: Identity) -> Identity | None:
        """Where a message to ``target`` would be delivered, resolved but not sent."""
        return self._destination(target)

    def _destination(self, target: Identity) -> Identity | None:
        if self._resolver is None:
            return target

        try:
            return self._resolver(target)
        except Exception:
            # Whatever went wrong, the message cannot be delivered, and the
            # reason belongs in the log next to the message that failed.
            self.logger.exception("Could not resolve a destination for %s", target)
            return None


async def tell_admins(notifier, users, text: str) -> None:
    for user in users.active_admins():
        try:
            await notifier.deliver(users.identity_for(user), text)
        except Exception:
            logger.exception("Could not tell %s", user.username)


class LinkedChatResolver:
    """Answers where a web account's notifications should go.

    The link says two identities are the same person, so a reminder created on
    the web is delivered over Telegram. Web data stays under ``web:<name>``:
    what moves here is the delivery, not the storage.

    A web identity with no link resolves to itself, which reaches
    :class:`WebChannel`. That is the pre-existing behaviour — logged and
    dropped — rather than an error, so an unlinked deployment keeps working
    exactly as it did.
    """

    def __init__(self, users, links, push=None):
        self.logger = logging.getLogger(__name__)
        self._users = users
        self._links = links
        self._push = push

    def __call__(self, target: Identity) -> Identity:
        if target.kind == TELEGRAM:
            return self._web_with_devices(target) or target

        if target.kind != WEB:
            return target

        user = self._users.get_by_username(target.id)

        if user is None:
            return target

        if self._push is not None and self._push.subscriptions(target):
            return target

        link = self._links.get_by_user_id(user.id)

        if link is None:
            self.logger.warning(
                "No Telegram link for %s; its notification has nowhere to go",
                target,
            )
            return target

        return Identity(kind="telegram", id=str(link.telegram_id))

    def _web_with_devices(self, target: Identity) -> Identity | None:
        if self._push is None or not target.id.lstrip("-").isdigit():
            return None

        link = self._links.get_by_telegram_id(int(target.id))
        user = self._users.get_by_id(link.user_id) if link else None

        if user is None:
            return None

        web = self._users.identity_for(user)

        return web if self._push.subscriptions(web) else None


def within(window: tuple[time, time], moment: time) -> bool:
    start, end = window

    if start <= end:
        return start <= moment < end

    return moment >= start or moment < end


class QuietHours:
    def __init__(self, users, links, held, clock=None):
        self._users = users
        self._links = links
        self._held = held
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> time:
        local = self._clock().astimezone(ZoneInfo(Config.DISPLAY_TIMEZONE))
        return local.time().replace(second=0, microsecond=0)

    def _account(self, target: Identity):
        if target.kind == WEB:
            return self._users.get_by_username(target.id)

        if target.kind == TELEGRAM:
            link = self._links.get_by_telegram_id_or_none(target.id)
            return self._users.get_by_id(link.user_id) if link else None

        return None

    def _quiet(self, user_id: int) -> bool:
        window = self._users.quiet_hours(user_id)
        return window is not None and within(window, self._now())

    def hold(self, target: Identity, text: str, url: str, buttons) -> bool:
        user = self._account(target)

        if user is None or not self._quiet(user.id):
            return False

        self._held.hold(
            user.id,
            str(target),
            {"text": text, "url": url, "buttons": [list(b) for b in buttons]},
        )

        return True

    def releasable(self) -> list[tuple[int, Identity, dict, bool]]:
        quiet: dict[int, bool] = {}
        ready = []
        cutoff = self._clock().astimezone(UTC).replace(tzinfo=None) - HELD_GIVE_UP

        for held_id, user_id, target, body, created_at in self._held.entries():
            if user_id not in quiet:
                quiet[user_id] = self._quiet(user_id)
            if not quiet[user_id]:
                ready.append(
                    (held_id, Identity.parse(target), body, created_at < cutoff)
                )

        return ready

    def release(self, held_id: int) -> None:
        self._held.release(held_id)
