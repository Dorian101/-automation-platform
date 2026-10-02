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

    def __init__(self, database: Database | None = None):
        self.repo = ClipboardRepository(database)

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

        text = self.repo.get_last(identity)

        if not text:
            return reply("Clipboard is empty")

        return reply(text)
