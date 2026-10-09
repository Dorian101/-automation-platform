import logging
from typing import Protocol

from app.core.identity import WEB, Identity

logger = logging.getLogger("platform")


class NotificationChannel(Protocol):
    """Delivers a message to a user over one specific transport."""

    kind: str

    async def deliver(self, target: Identity, text: str) -> None: ...


class TelegramChannel:
    kind = "telegram"

    def __init__(self, bot):
        self._bot = bot

    async def deliver(self, target: Identity, text: str) -> None:
        await self._bot.send_message(chat_id=int(target.id), text=text)


class WebChannel:
    """Placeholder channel for users who arrived through the web interface.

    Web users have no inbox yet, so delivery is logged instead of sent. The
    signature matches :class:`NotificationChannel`, so giving the web interface
    a real inbox is a matter of replacing this class.
    """

    kind = WEB

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def deliver(self, target: Identity, text: str) -> None:
        self.logger.info("Dropping web notification for %s: %s", target, text)


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
    ):
        self.logger = logging.getLogger(__name__)
        self._channels = {channel.kind: channel for channel in channels}
        self._resolver = resolver

    def kinds(self) -> list[str]:
        return list(self._channels)

    async def deliver(self, target: Identity, text: str) -> bool:
        """Deliver, reporting whether the message actually went anywhere.

        The caller needs the difference between "delivered" and "handled but
        not delivered": a message nobody could receive must not be retried for
        ever, and only the caller knows whether giving up is allowed.
        """
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

        await channel.deliver(destination, text)

        return True

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

    def __init__(self, users, links):
        self.logger = logging.getLogger(__name__)
        self._users = users
        self._links = links

    def __call__(self, target: Identity) -> Identity:
        if target.kind != WEB:
            return target

        user = self._users.get_by_username(target.id)

        if user is None:
            return target

        link = self._links.get_by_user_id(user.id)

        if link is None:
            self.logger.warning(
                "No Telegram link for %s; its notification has nowhere to go",
                target,
            )
            return target

        return Identity(kind="telegram", id=str(link.telegram_id))
