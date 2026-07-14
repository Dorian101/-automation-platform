import asyncio
import os

from dotenv import load_dotenv

from app.core.logger import setup_logger
from app.core.config import Config

from app.bot.bot import create_bot, create_dispatcher
from app.bot.error_handler import register_error_handler

from app.plugins.manager import PluginManager
from app.plugins.notes import NotesPlugin
from app.plugins.reminders import RemindersPlugin
from app.plugins.clipboard import ClipboardPlugin
from app.plugins.system import SystemPlugin

load_dotenv()

async def main():
    logger = setup_logger()

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN is not set")
        return

    bot = create_bot(token)
    dp = create_dispatcher()
    
    register_error_handler(dp)
	
    manager = PluginManager(logger)
    manager.register(NotesPlugin())
    manager.register(RemindersPlugin(bot))
    manager.register(ClipboardPlugin())
    
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