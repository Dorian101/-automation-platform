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

    def __init__(self, database: Database | None = None, persons=None):
        self.repo = NotesRepository(database)
        self._persons = persons

    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        if command == "/add":
            if not args:
                raise CommandError("Usage: /add <text>")

            # Written under the identity that wrote it, never widened: the
            # stored row says who wrote it and from where, and unlinking takes
            # effect at once because there is nothing to undo.
            self.repo.add(identity, args)

            return reply("Saved")

        notes = self.repo.list(self._readable_as(identity))

        if not notes:
            return reply("No notes")

        return reply("\n".join(f"- {note}" for note in notes))

    def _readable_as(self, identity: Identity):
        """The identities whose notes are this person's to read.

        Falls back to the identity alone, so a plugin built without a resolver
        behaves exactly as it did before pairing existed.
        """
        if self._persons is None:
            return identity

        return self._persons.identities(identity)
