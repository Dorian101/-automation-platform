from datetime import UTC, datetime

from app.core.identity import Identity
from app.core.results import CommandError, CommandResult, reply
from app.db.clipboard_repo import ClipboardRepository
from app.db.database import Database

from .base import BasePlugin


class ClipboardPlugin(BasePlugin):
    name = "clipboard"
    version = "1.1.0"
    description = "Store and retrieve clipboard entries"
    commands = {
        "/copy": "Save text to clipboard",
        "/paste": "Get last copied text",
    }

    def __init__(self, database: Database | None = None, persons=None):
        self.repo = ClipboardRepository(database)
        self._persons = persons

    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        if command == "/copy":
            if not args:
                raise CommandError("Usage: /copy <text>")

            self.repo.save(
                identity=identity,
                text=args,
                created_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
            )

            return reply("Copied")

        # One buffer for both entry points: pasting in the bot returns what was
        # copied on the web, and the other way round. Saving is not widened, so
        # the row still records which transport the text arrived through.
        text = self.repo.get_last(self._readable_as(identity))

        if not text:
            return reply("Clipboard is empty")

        return reply(text)

    def _readable_as(self, identity: Identity):
        if self._persons is None:
            return identity

        return self._persons.identities(identity)
