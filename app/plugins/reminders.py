import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.core.identity import Identity
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.reminders_repo import RemindersRepository
from app.notifications import Notifier

from .base import BasePlugin

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


class RemindersPlugin(BasePlugin):
    name = "reminders"
    version = "1.1.0"
    description = "Schedule reminders"
    commands = {
        "/remind": "Create a reminder",
    }

    def __init__(self, notifier: Notifier, database: Database | None = None):
        self.repo = RemindersRepository(database)
        self._notifier = notifier
        self._worker_task: asyncio.Task | None = None

    async def on_startup(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())

    async def on_shutdown(self) -> None:
        if not self._worker_task:
            return

        self._worker_task.cancel()

        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass

        self._worker_task = None

    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        minutes_str, _, text = args.partition(" ")

        if not text.strip():
            raise CommandError("Usage: /remind <minutes> <text>")

        try:
            minutes = int(minutes_str)
        except ValueError:
            raise CommandError("Minutes must be a number") from None

        if minutes < 0:
            raise CommandError("Minutes must not be negative")

        remind_at = (
            datetime.now(UTC) + timedelta(minutes=minutes)
        ).replace(tzinfo=None).isoformat()

        self.repo.add(identity, text.strip(), remind_at)

        return reply(f"Reminder set in {minutes} min")

    async def _worker(self) -> None:
        while True:
            for reminder_id, user_id, text in self.repo.get_due():
                try:
                    target = Identity.parse(user_id)
                except ValueError:
                    logger.warning(
                        "Reminder %s has an unparsable user_id %r",
                        reminder_id,
                        user_id,
                    )
                    continue

                try:
                    await self._notifier.deliver(target, text)
                except Exception:
                    logger.exception(
                        "Failed to deliver reminder %s to %s",
                        reminder_id,
                        target,
                    )
                    continue

                self.repo.mark_sent(reminder_id)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
