import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.core.identity import TELEGRAM, WEB, Identity
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
    description = "Напоминания"
    commands = {
        "/remind": "Создать напоминание",
        "/reminders": "Показать напоминания",
        "/cancel": "Отменить напоминание по номеру",
    }
    command_args = {
        "/remind": [("number", "Минуты"), ("text", "Текст")],
        "/cancel": [("number", "Номер")],
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
        if command == "/reminders":
            return self._list(identity)

        if command == "/cancel":
            return self._cancel(args, identity)

        minutes_str, _, text = args.partition(" ")

        if not text.strip():
            raise CommandError("Usage: /remind <minutes> <text>")

        try:
            minutes = int(minutes_str)
        except ValueError:
            raise CommandError("Minutes must be a number") from None

        if minutes < 0:
            raise CommandError("Minutes must not be negative")

        # A web account with no Telegram link has nowhere for the reminder to
        # go: WebChannel only logs and drops. Refused up front rather than
        # letting it sit long enough to be given up on — the message tells the
        # person why, and the command fails instead of quietly delivering
        # nothing.
        destination = self._notifier.destination(identity)
        if identity.kind == WEB and (
            destination is None or destination.kind != TELEGRAM
        ):
            raise CommandError(
                "Reminders are delivered to Telegram, and this web account is "
                "not linked. Link your Telegram account in the console first."
            )

        remind_at = (
            datetime.now(UTC) + timedelta(minutes=minutes)
        ).replace(tzinfo=None).isoformat()

        self.repo.add(identity, text.strip(), remind_at)

        return reply(f"Reminder set in {minutes} min")

    def _list(self, identity: Identity) -> CommandResult:
        reminders = self.repo.entries(identity)

        if not reminders:
            return reply("No reminders")

        lines = ["Reminders (soonest first):"]
        for position, (_, text, remind_at) in enumerate(reminders, 1):
            lines.append(
                f"{position}. {_format_remind_at(remind_at)} — {text}"
            )

        return reply("\n".join(lines))

    def _cancel(self, args: str, identity: Identity) -> CommandResult:
        number = args.strip()

        try:
            position = int(number)
        except ValueError:
            raise CommandError("Usage: /cancel <number>") from None

        if position < 1:
            raise CommandError("Usage: /cancel <number>")

        reminders = self.repo.entries(identity)

        if position > len(reminders):
            raise CommandError(
                f"No reminder {position}. Use /reminders to see {len(reminders)}.",
            )

        reminder_id = reminders[position - 1][0]

        if not self.repo.delete(identity, reminder_id):
            # Only reachable if the list changed between the two calls.
            raise CommandError(
                f"No reminder {position}. Use /reminders to see the list.",
            )

        return reply(f"Cancelled reminder {position}")

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


def _format_remind_at(remind_at: datetime) -> str:
    """The reminder's moment, in the reader's local time.

    ``remind_at`` comes back from the database as a naive UTC timestamp. It is
    rendered local because a person comparing "soon" against the clock on the
    wall should not have to convert; the offset is named so the reader is not
    left to guess which clock a bare number is on.
    """
    local = remind_at.replace(tzinfo=UTC).astimezone()
    zone = local.tzname() or local.strftime("%z")

    return f"{local.strftime('%Y-%m-%d %H:%M')} {zone}"
