from aiogram import Router
from aiogram.types import Message

from .base import BasePlugin
from db.notes_repo import NotesRepository


class NotesPlugin(BasePlugin):
    name = "notes"
    version = "1.0.0"
    description = "Store personal notes"
    commands = {
    "/add": "Add a new note",
    "/notes": "Show all notes",
    }

    def __init__(self):
        self.repo = NotesRepository()

    def router(self) -> Router:
        router = Router()

        @router.message(lambda m: m.text and m.text.startswith("/add "))
        async def add_note(message: Message):
            note = message.text.replace("/add ", "", 1).strip()

            if not note:
                await message.answer("Empty note")
                return

            self.repo.add(note)
            await message.answer("Saved")

        @router.message(lambda m: m.text == "/notes")
        async def list_notes(message: Message):
            notes = self.repo.list()

            if not notes:
                await message.answer("No notes")
                return

            text = "\n".join(f"- {n}" for n in notes)
            await message.answer(text)

        return router