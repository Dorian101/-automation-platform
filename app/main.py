import asyncio
import os

from core.logger import setup_logger
from core.config import Config

from bot.bot import create_bot, create_dispatcher

from plugins.manager import PluginManager
from plugins.echo import EchoPlugin
from plugins.notes import NotesPlugin
from plugins.reminders import RemindersPlugin
from plugins.system import SystemPlugin


async def main():
    logger = setup_logger()

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN is not set")
        return

    bot = create_bot(token)
    dp = create_dispatcher()
    reminders = RemindersPlugin(bot)
	
    manager = PluginManager()
    manager.register(NotesPlugin())
    manager.register(reminders)
    manager.register(SystemPlugin(manager))
    manager.setup(dp)
    
    await manager.startup()

    logger.info(f"Starting {Config.APP_NAME}")

    try:
        await dp.start_polling(bot)
    finally:
        await manager.shutdown()

if __name__ == "__main__":
    asyncio.run(main())