from typing import Sequence

from aiogram import Router
from aiogram.types import Message

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
        return [
            {
                "name": plugin.name,
                "page": plugin.page,
                "title": plugin.page_title or plugin.name,
                "description": plugin.description,
            }
            for plugin in self._plugins
            if plugin.page
        ]

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
        """
        router = Router(name="platform")

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

        for plugin in self._plugins:
            plugin_router = plugin.router()

            if plugin_router.message.handlers:
                router.include_router(plugin_router)

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
