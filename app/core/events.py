import logging
from dataclasses import dataclass, field

from app.core.identity import Identity

logger = logging.getLogger(__name__)

MAX_CHAIN = 3


@dataclass(frozen=True)
class Event:
    name: str
    identity: Identity
    data: dict = field(default_factory=dict)
    chain: tuple[int, ...] = ()


class EventBus:
    def __init__(self):
        self._handlers = []

    def subscribe(self, handler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: Event) -> None:
        for handler in self._handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception("Event handler failed on %s", event.name)
