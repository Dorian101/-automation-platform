import contextlib
import logging

from aiogram import Dispatcher
from aiogram.types import ErrorEvent

logger = logging.getLogger("platform")

FAILURE_REPLY = "Что-то пошло не так. Попробуйте ещё раз чуть позже."


def register_error_handler(dp: Dispatcher):
    @dp.error()
    async def global_error_handler(event: ErrorEvent):
        logger.exception(
            "Unhandled exception while processing update",
            exc_info=event.exception,
        )

        message = event.update.message
        if message is not None:
            with contextlib.suppress(Exception):
                await message.answer(FAILURE_REPLY)

        return True
