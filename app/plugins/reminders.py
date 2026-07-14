import asyncio
from datetime import datetime, timedelta

from aiogram import Router
from aiogram.types import Message
from datetime import datetime, timedelta

from .base import BasePlugin
from app.db.reminders_repo import RemindersRepository


class RemindersPlugin(BasePlugin):
    name = "reminders"
    version = "1.0.0"
    description = "Schedule reminders"
    commands = {
    "/remind": "Create a reminder",
    }

    def __init__(self, bot):
        self.repo = RemindersRepository()
        self.bot = bot
        self._worker_task = None

    async def on_startup(self):
        self._worker_task = asyncio.create_task(self.worker())


    async def on_shutdown(self):
        if self._worker_task:
            self._worker_task.cancel()

            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    def router(self) -> Router:
        router = Router()

        @router.message(lambda m: m.text and m.text.startswith("/remind "))
        async def add_reminder(message: Message):
            try:
                parts = message.text.strip().split(maxsplit=2)

                if len(parts) < 3:
                    await message.answer("Format: /remind <minutes> <text>")
                    return

                _, minutes, text = parts
                minutes = int(minutes)

                remind_at = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()

                self.repo.add(message.chat.id, text, remind_at)

                await message.answer(f"Reminder set in {minutes} min")

            except ValueError:
                await message.answer("Minutes must be a number")

        return router
    async def worker(self):
        while True:
            due = self.repo.get_due()

            for reminder_id,chat_id, text in due:
                try:
                    await self.bot.send_message(chat_id=chat_id, text=text)
                    self.repo.mark_sent(reminder_id)
                except Exception:
                    pass

            await asyncio.sleep(30)