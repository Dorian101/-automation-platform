from aiogram import Router
from aiogram.types import Message

from .base import BasePlugin


class EchoPlugin(BasePlugin):
    name = "echo"

    def router(self) -> Router:
        router = Router()

        @router.message()
        async def echo(message: Message):
            await message.answer(message.text)

        return router