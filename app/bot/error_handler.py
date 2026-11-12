import contextlib
import logging

from aiogram import Dispatcher
from aiogram.types import ErrorEvent

from app.core.results import CommandError

logger = logging.getLogger("platform")

FAILURE_REPLY = "Что-то пошло не так. Попробуйте ещё раз чуть позже."


def register_error_handler(dp: Dispatcher):
    @dp.error()
    async def global_error_handler(event: ErrorEvent):
        refused = isinstance(event.exception, CommandError)

        if not refused:
            logger.exception(
                "Unhandled exception while processing update",
                exc_info=event.exception,
            )

        message = event.update.message
        if message is not None:
            reply = str(event.exception) if refused else FAILURE_REPLY
            with contextlib.suppress(Exception):
                await message.answer(reply)

        return True
