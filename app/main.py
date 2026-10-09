import asyncio

from app.bot.bot import create_bot, create_dispatcher
from app.bot.error_handler import register_error_handler
from app.core.config import Config
from app.core.logger import setup_logger
from app.db.database import Database
from app.db.migrations import MigrationRunner
from app.notifications import Notifier, TelegramChannel, WebChannel
from app.plugins.clipboard import ClipboardPlugin
from app.plugins.manager import PluginManager
from app.plugins.notes import NotesPlugin
from app.plugins.reminders import RemindersPlugin
from app.plugins.system import SystemPlugin
from app.web import WebServer

# sysexits EX_CONFIG: the process is misconfigured and retrying will not help.
# Distinct from other failures so systemd can keep restarting those.
EX_CONFIG = 78


async def main() -> None:
    logger = setup_logger()

    token = Config.BOT_TOKEN
    if not token:
        logger.error("BOT_TOKEN is not set")
        raise SystemExit(EX_CONFIG)

    bot = create_bot(token)
    dp = create_dispatcher()

    database = Database()
    MigrationRunner(database).run()

    register_error_handler(dp)

    notifier = Notifier([TelegramChannel(bot), WebChannel()])

    manager = PluginManager(logger)
    manager.register(NotesPlugin(database))
    manager.register(RemindersPlugin(notifier, database))
    manager.register(ClipboardPlugin(database))
    manager.register(SystemPlugin(manager))

    dp.include_router(manager.build_router())

    web = WebServer(
        manager=manager,
        database=database,
        host=Config.WEB_HOST,
        port=Config.WEB_PORT,
    )

    await manager.startup()

    logger.info("Starting %s", Config.APP_NAME)

    await web.start()

    try:
        await dp.start_polling(bot)
    finally:
        await web.stop()
        await manager.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
