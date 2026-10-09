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

# One unreachable destination must not fill the journal for ever. A chat that
# was never linked is a configuration mistake, not a transient failure, and
# retrying it every half minute forever turns one missing line in .env into a
# log nobody will read. A delivery that raises is left to be retried, since
# that one may succeed; a delivery that resolves to nothing is not retried.
MAX_DROPPED_ATTEMPTS = 3


class RemindersPlugin(BasePlugin):
    name = "reminders"
    version = "1.2.0"
    description = "Schedule reminders"
    commands = {
        "/remind": "Create a reminder",
    }

    def __init__(self, notifier: Notifier, database: Database | None = None):
        self.repo = RemindersRepository(database)
        self._notifier = notifier
        self._worker_task: asyncio.Task | None = None
        self._dropped_attempts: dict[int, int] = {}

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
            await self.process_due()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def process_due(self) -> None:
        """Deliver everything that has come due.

        Separate from the loop so the decision this makes — deliver, retry, or
        give up — can be exercised without a task that never returns.
        """
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
                delivered = await self._notifier.deliver(target, text)
            except Exception:
                # The message was never delivered, so the reminder stays due
                # and is tried again on the next pass. A Telegram outage is
                # exactly the case this is for.
                logger.exception(
                    "Failed to deliver reminder %s to %s",
                    reminder_id,
                    target,
                )
                continue

            if not delivered and not self._give_up_on(reminder_id, target):
                continue

            self.repo.mark_sent(reminder_id)
            self._dropped_attempts.pop(reminder_id, None)

    def _give_up_on(self, reminder_id: int, target: Identity) -> bool:
        """Count an undeliverable reminder, reporting whether to stop.

        Nobody was reached, so retrying cannot help until the account is linked
        — and trying every thirty seconds regardless is how one unconfigured
        account becomes a journal nobody reads. A delivery that raised instead
        never reaches here, because an outage is worth waiting out.
        """
        attempts = self._dropped_attempts.get(reminder_id, 0) + 1
        self._dropped_attempts[reminder_id] = attempts

        if attempts < MAX_DROPPED_ATTEMPTS:
            return False

        logger.warning(
            "Giving up on reminder %s to %s after %s attempts with nowhere "
            "to deliver it",
            reminder_id,
            target,
            attempts,
        )

        return True
