from aiogram import Router
from aiogram.types import Message

from .base import BasePlugin


class NotesPlugin(BasePlugin):
    name = "notes"

    def __init__(self):
        self._notes: list[str] = []

    def router(self) -> Router:
        router = Router()

        @router.message(lambda m: m.text and m.text.startswith("/add "))
        async def add_note(message: Message):
            note = message.text.replace("/add ", "", 1).strip()
            if not note:
                await message.answer("Empty note")
                return

            self._notes.append(note)
            await message.answer("Saved")

        @router.message(lambda m: m.text == "/notes")
        async def list_notes(message: Message):
            if not self._notes:
                await message.answer("No notes")
                return

            text = "\n".join(f"- {n}" for n in self._notes)
            await message.answer(text)

        return router