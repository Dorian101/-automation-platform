from app.core.identity import Identity
from app.core.results import CommandError, CommandResult, reply
from app.db.database import Database
from app.db.notes_repo import NotesRepository

from .base import BasePlugin


def _rows_payload(notes: list[tuple[int, str]]) -> dict:
    """The note list in the shape the web console knows how to draw.

    Columns carry their own titles so the console does not have to know what a
    note is — it places ``label`` above each column and leaves the rest to the
    plugin that filled them in. The position is exported as a value rather than
    as row ids, because the id is deliberately never shown and a console must
    not be the place it starts leaking.
    """
    return {
        "kind": "rows",
        "draw": "table",
        "columns": [
            {"key": "position", "label": "#"},
            {"key": "text", "label": "Note"},
        ],
        "rows": [
            {"position": position, "text": text}
            for position, (_, text) in enumerate(notes, 1)
        ],
    }


class NotesPlugin(BasePlugin):
    name = "notes"
    version = "1.2.0"
    description = "Store personal notes"
    commands = {
        "/add": "Add a new note",
        "/notes": "Show all notes",
        "/del": "Delete a note by number",
        "/clear": "Delete every note",
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

        if command == "/del":
            return self._delete(args, identity)

        if command == "/clear":
            return self._clear(identity)

        return self._list(identity)

    def _list(self, identity: Identity) -> CommandResult:
        notes = self.repo.entries(self._readable_as(identity))

        if not notes:
            return reply("No notes")

        # Numbered by position, not by row id. The row id is global across
        # every user, so printing it would leak how many notes other people
        # have, and it would let a guess address someone else's note. A position
        # is only meaningful to the person looking at this list anyway.
        return reply(
            "\n".join(
                f"{position}. {text}" for position, (_, text) in enumerate(notes, 1)
            ),
            data=_rows_payload(notes),
        )

    def _delete(self, args: str, identity: Identity) -> CommandResult:
        number = args.strip()

        try:
            position = int(number)
        except ValueError:
            raise CommandError("Usage: /del <number>") from None

        if position < 1:
            raise CommandError("Usage: /del <number>")

        notes = self.repo.entries(self._readable_as(identity))

        if position > len(notes):
            raise CommandError(
                f"No note {position}. Use /notes to see {len(notes)}.",
            )

        note_id = notes[position - 1][0]

        if not self.repo.delete(self._readable_as(identity), note_id):
            # Only reachable if the list changed between the two calls. Said as
            # "not found" rather than as a race, because that is what the person
            # needs to do about it.
            raise CommandError(f"No note {position}. Use /notes to see the list.")

        return reply(f"Deleted note {position}")

    def _clear(self, identity: Identity) -> CommandResult:
        removed = self.repo.clear(self._readable_as(identity))

        if not removed:
            return reply("No notes to delete")

        if removed == 1:
            return reply("Deleted 1 note")

        return reply(f"Deleted {removed} notes")

    def _readable_as(self, identity: Identity):
        """The identities whose notes are this person's to read and delete.

        Widening a read without widening deletion would leave a note visible in
        the console and impossible to remove from it.

        Falls back to the identity alone, so a plugin built without a resolver
        behaves exactly as it did before pairing existed.
        """
        if self._persons is None:
            return identity

        return self._persons.identities(identity)
