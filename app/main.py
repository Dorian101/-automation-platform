import asyncio
import os

from core.logger import setup_logger
from core.config import Config

from bot.bot import create_bot, create_dispatcher

from plugins.manager import PluginManager
from plugins.echo import EchoPlugin
from plugins.notes import NotesPlugin


async def main():
    logger = setup_logger()

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN is not set")
        return

    bot = create_bot(token)
    dp = create_dispatcher()

    manager = PluginManager()
    #manager.register(EchoPlugin())
    manager.register(NotesPlugin())
    manager.setup(dp)

    logger.info(f"Starting {Config.APP_NAME}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())