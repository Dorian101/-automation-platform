from typing import Sequence

from aiogram import Router
from aiogram.types import BotCommand, Message

from app.bot.menu import build_menu_router
from app.core.commands import parse_command
from app.core.identity import WEB, Identity, telegram
from app.core.results import CommandError, CommandResult

from .base import BasePlugin


class PluginManager:
    def __init__(self, logger):
        self._plugins: list[BasePlugin] = []
        self.logger = logger

    def register(self, plugin: BasePlugin) -> None:
        self._plugins.append(plugin)

        self.logger.info(
            "Registered plugin: %s",
            plugin.name,
        )

    def get_plugins(self) -> Sequence[BasePlugin]:
        return self._plugins.copy()

    def find(self, command: str) -> BasePlugin | None:
        for plugin in self._plugins:
            if command in plugin.commands:
                return plugin

        return None

    def find_by_page(self, page: str) -> BasePlugin | None:
        """The plugin that owns a web page, if one does.

        Matched on the declared path rather than looked up by name, so a route
        is only reachable through the plugin that asked for it.
        """
        for plugin in self._plugins:
            if plugin.page == page:
                return plugin

        return None

    def pages(self) -> list[dict]:
        """Every plugin that has a page, for the navigation."""
        return self._page_entries()

    def demo_pages(self) -> list[dict]:
        """Only the pages that belong on the public demonstration.

        A separate answer rather than a filter on :meth:`pages`: the owner's
        menu wants everything and a guest's wants the published subset, and
        having one of them filter at the call site means the rule about what is
        public lives in the web layer, where nothing declares it.
        """
        return [entry for entry in self._page_entries() if entry["demo"]]

    def _page_entries(self) -> list[dict]:
        return [
            {
                "name": plugin.name,
                "page": plugin.page,
                "title": plugin.page_title or plugin.name,
                "description": plugin.description,
                "demo": plugin.demo,
            }
            for plugin in self._plugins
            if plugin.page
        ]

    def _commands_in_order(self) -> list[tuple[BasePlugin, str, str]]:
        """Every command, in plugin order, as (plugin, command, description)."""
        rows = []

        for plugin in self._plugins:
            for command, description in plugin.commands.items():
                rows.append((plugin, command, description))

        return rows

    def bot_commands(self) -> list[BotCommand]:
        """The native Telegram command menu, shown when the user types "/".

        One flat list of every command a chat can actually run, with the
        argument form folded into the description so a reader who has never
        seen the bot knows what to type. Web-only plugins are skipped: their
        commands answer "web console only", so offering them in a chat menu is
        offering something that cannot work.
        """
        commands = []

        for plugin, command, description in self._commands_in_order():
            if plugin.web_only:
                continue

            commands.append(
                BotCommand(command=command, description=description)
            )

        return commands

    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        plugin = self.find(command)

        if plugin is None:
            raise CommandError(f"Unknown command: {command}")

        if plugin.web_only and identity.kind != WEB:
            # Refused here rather than inside the plugin: the transport is the
            # router's business, and a plugin carrying that check would have to
            # know it had two of them.
            raise CommandError(
                f"{command} is available on the web console only.",
            )

        return await plugin.execute(command, args, identity)

    def build_router(self) -> Router:
        """Build the Telegram router for the whole platform.

        Declared commands are dispatched generically so that plugins do not
        repeat adapter code. Plugins needing transport-specific behaviour
        contribute their own router through :meth:`BasePlugin.router`.

        The button menu and the step-by-step input flow run first
        (:func:`app.bot.menu.build_menu_router`); the generic text parser runs
        last, so a bare message like "Заметка" reaches the menu rather than
        falling into the command parser and failing on the missing "/".
        """
        router = Router(name="platform")

        # The menu first, then the plugins' own handlers, then the generic
        # text parser. Order matters: aiogram matches in registration order.
        menu = build_menu_router(self.execute)
        router.include_router(menu)

        for plugin in self._plugins:
            plugin_router = plugin.router()

            if plugin_router.message.handlers:
                router.include_router(plugin_router)

        @router.message()
        async def dispatch(message: Message) -> None:
            if not message.text:
                return

            try:
                command, args = parse_command(message.text)
                identity = telegram(message.chat.id)
                result = await self.execute(command, args, identity)
            except CommandError as error:
                await message.answer(str(error))
                return

            if result.text:
                await message.answer(result.text)

        return router

    async def startup(self) -> None:
        for plugin in self._plugins:
            await plugin.on_startup()

            self.logger.info(
                "Started plugin: %s",
                plugin.name,
            )

    async def shutdown(self) -> None:
        for plugin in reversed(self._plugins):
            await plugin.on_shutdown()

            self.logger.info(
                "Stopped plugin: %s",
                plugin.name,
            )
