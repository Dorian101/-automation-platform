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
    """Routes a message to the channel matching the target's transport."""

    def __init__(self, channels: list[NotificationChannel]):
        self.logger = logging.getLogger(__name__)
        self._channels = {channel.kind: channel for channel in channels}

    def kinds(self) -> list[str]:
        return list(self._channels)

    async def deliver(self, target: Identity, text: str) -> None:
        channel = self._channels.get(target.kind)

        if channel is None:
            self.logger.warning("No channel for transport %r", target.kind)
            return

        await channel.deliver(target, text)
