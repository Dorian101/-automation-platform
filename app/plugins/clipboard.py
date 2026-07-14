from datetime import datetime

from aiogram import Router
from aiogram.types import Message

from .base import BasePlugin
from app.db.clipboard_repo import ClipboardRepository


class ClipboardPlugin(BasePlugin):
    name = "clipboard"
    version = "1.0.0"
    description = "Useless plugin, cause it sends text to yours chat_id, but you already can see sended Copy command with target text"

    commands = {
        "/copy": "Save text to clipboard",
        "/paste": "Get last copied text",
    }

    def __init__(self):
        self.repo = ClipboardRepository()

    def router(self) -> Router:
        router = Router()

        @router.message(lambda m: m.text and m.text.startswith("/copy"))
        async def copy_command(message: Message):
            text = message.text.removeprefix("/copy").strip()
            
            if not text:
                await message.answer("Usage: /copy <text>")
                return

            self.repo.save(
                text=text,
                created_at=datetime.utcnow().isoformat(),
                chat_id = message.chat.id,
            )

            await message.answer("Copied")

        @router.message(lambda m: m.text == "/paste")
        async def paste_command(message: Message):
            text = self.repo.get_last(message.chat.id)

            if not text:
                await message.answer("Clipboard is empty")
                return

            await message.answer(text)

        return router