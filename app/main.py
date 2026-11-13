import asyncio
import logging
from contextlib import suppress

from app.bot.bot import create_bot, create_dispatcher
from app.bot.error_handler import register_error_handler
from app.core.config import Config
from app.core.logger import setup_logger
from app.core.person import PersonResolver
from app.db.backup import BackupManager
from app.db.database import Database
from app.db.errors_repo import ErrorLogHandler, ErrorsRepository
from app.db.held_repo import HeldNotificationsRepository
from app.db.links_repo import TelegramLinksRepository
from app.db.migrations import MigrationRunner
from app.db.push_repo import PushRepository
from app.db.users_repo import UsersRepository
from app.notifications import (
    LinkedChatResolver,
    Notifier,
    QuietHours,
    TelegramChannel,
    WebPushChannel,
)
from app.plugins.applications import ApplicationsPlugin
from app.plugins.automation import AutomationPlugin
from app.plugins.bookmarks import BookmarksPlugin
from app.plugins.collections import CollectionsPlugin
from app.plugins.expenses import ExpensesPlugin
from app.plugins.manager import PluginManager
from app.plugins.notes import NotesPlugin
from app.plugins.reminders import RemindersPlugin
from app.plugins.snippets import SnippetsPlugin
from app.plugins.system import SystemPlugin
from app.plugins.todo import TodoPlugin
from app.watch import Watch
from app.web import WebServer
from app.web.admin import ERRORS_PATH
from app.web.passkeys import rp_id

# sysexits EX_CONFIG: the process is misconfigured and retrying will not help.
# Distinct from other failures so systemd can keep restarting those.
EX_CONFIG = 78


def build_manager(database, notifier, users, links, logger) -> PluginManager:
    persons = PersonResolver(users, links)
    manager = PluginManager(logger)
    manager.register(NotesPlugin(database, persons=persons))
    manager.register(RemindersPlugin(notifier, database, persons=persons))
    manager.register(TodoPlugin(notifier, database, persons=persons))
    manager.register(SnippetsPlugin(database, persons=persons))
    manager.register(BookmarksPlugin(database, persons=persons))
    manager.register(ExpensesPlugin(database, persons=persons))
    manager.register(ApplicationsPlugin(database, persons=persons))
    manager.register(CollectionsPlugin(database, persons=persons))
    manager.register(AutomationPlugin(manager, notifier, database, persons=persons))
    manager.register(SystemPlugin(manager, users=users, links=links))
    return manager


async def main() -> None:
    logger = setup_logger()

    token = Config.BOT_TOKEN
    if not token:
        logger.error("BOT_TOKEN is not set")
        raise SystemExit(EX_CONFIG)

    bot = create_bot(token)
    dp = create_dispatcher()

    database = Database()
    # A backup manager here and nowhere else: this is the one path that
    # changes the schema on a running system, so it is the one that gets a
    # dump before it does. The management commands apply migrations too, but
    # by hand, on a host an operator is sitting in front of.
    MigrationRunner(database, backup=BackupManager()).run()

    # After the migrations, so the table exists. Errors from here on are also
    # readable on /admin/errors, which the journal is not to this user.
    error_log = ErrorLogHandler(ErrorsRepository(database))
    logging.getLogger().addHandler(error_log)

    register_error_handler(dp)

    users = UsersRepository(database)
    links = TelegramLinksRepository(database)
    push = PushRepository(database)

    web_push = WebPushChannel(push, Config.VAPID_PRIVATE_KEY, Config.VAPID_SUBJECT)
    notifier = Notifier(
        [TelegramChannel(bot), web_push],
        resolver=LinkedChatResolver(users, links, push),
        quiet=QuietHours(users, links, HeldNotificationsRepository(database)),
    )

    manager = build_manager(database, notifier, users, links, logger)

    dp.include_router(manager.build_router())

    # The native command menu Telegram shows at "/" is set once, ahead of
    # polling, so it does not need a handler of its own. It is the same command
    # list the menu buttons draw from — one source, two Telegram surfaces.
    await bot.set_my_commands(manager.bot_commands())

    web = WebServer(
        manager=manager,
        database=database,
        host=Config.WEB_HOST,
        port=Config.WEB_PORT,
        notifier=notifier,
    )
    loop = asyncio.get_running_loop()

    def errors_changed() -> None:
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(web.admin_changed, ERRORS_PATH)

    error_log.on_saved = errors_changed
    web_push.on_forgotten = web.console_changed

    await manager.startup()

    logger.info("Starting %s", Config.APP_NAME)

    await web.start()
    releaser = asyncio.create_task(notifier.run())
    watcher = asyncio.create_task(Watch(notifier, users, rp_id()).run())

    try:
        await dp.start_polling(bot)
    finally:
        releaser.cancel()
        watcher.cancel()
        await web.stop()
        await manager.shutdown()
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
