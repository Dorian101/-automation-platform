from app.core.identity import Identity
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.notes_repo import NotesRepository

from .base import BasePlugin


class NotesPlugin(BasePlugin):
    name = "notes"
    version = "1.1.0"
    description = "Store personal notes"
    commands = {
        "/add": "Add a new note",
        "/notes": "Show all notes",
    }

    def __init__(self, database: Database | None = None):
        self.repo = NotesRepository(database)

    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        if command == "/add":
            if not args:
                raise CommandError("Usage: /add <text>")

            self.repo.add(identity, args)

            return reply("Saved")

        notes = self.repo.list(identity)

        if not notes:
            return reply("No notes")

        return reply("\n".join(f"- {note}" for note in notes))
