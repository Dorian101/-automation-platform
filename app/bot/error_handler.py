import logging

from aiogram import Dispatcher
from aiogram.types import ErrorEvent

logger = logging.getLogger("platform")


def register_error_handler(dp: Dispatcher):
    @dp.error()
    async def global_error_handler(event: ErrorEvent):
        logger.exception(
            "Unhandled exception while processing update",
            exc_info=event.exception,
        )

        return True
