from io import BytesIO
from typing import Sequence

from aiogram import Router
from aiogram.types import BotCommand, BufferedInputFile, Message, MessageEntity

from app.bot.menu import build_menu_router
from app.core.commands import parse_command
from app.core.events import EventBus
from app.core.identity import WEB, Identity, telegram
from app.core.results import CommandError, CommandResult

from .base import BasePlugin


class PluginManager:
    def __init__(self, logger):
        self._plugins: list[BasePlugin] = []
        self.logger = logger
        # What the administrator switched: plugins turned off and maintenance.
        # An object with `disabled_plugins()` and `maintenance()`, handed in by
        # whoever owns the settings; None means nothing is ever switched.
        self.switches = None
        self.events = EventBus()

    def register(self, plugin: BasePlugin) -> None:
        plugin.events = self.events
        self._plugins.append(plugin)

        self.logger.info(
            "Registered plugin: %s",
            plugin.name,
        )

    def get_plugins(self) -> Sequence[BasePlugin]:
        """Every registered plugin, switched off or not."""
        return self._plugins.copy()

    def _enabled(self) -> list[BasePlugin]:
        disabled = self.switches.disabled_plugins() if self.switches else set()

        return [plugin for plugin in self._plugins if plugin.name not in disabled]

    def find(self, command: str) -> BasePlugin | None:
        for plugin in self._enabled():
            if command in plugin.commands:
                return plugin

        return None

    def find_action(self, action: str) -> BasePlugin | None:
        for plugin in self._enabled():
            if action in plugin.automation_actions:
                return plugin

        return None

    def automation_events(self) -> dict[str, tuple[str, list[str]]]:
        found = {}

        for plugin in self._enabled():
            found.update(plugin.automation_events)

        return found

    def automation_actions(self) -> dict[str, tuple[str, list[tuple[str, str]]]]:
        found = {}

        for plugin in self._enabled():
            found.update(plugin.automation_actions)

        return found

    def find_by_page(self, page: str) -> BasePlugin | None:
        """The plugin that owns a web page, if one does.

        Matched on the declared path rather than looked up by name, so a route
        is only reachable through the plugin that asked for it.
        """
        for plugin in self._enabled():
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
            for plugin in self._enabled()
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
        if self.switches and self.switches.maintenance():
            raise CommandError("Сервис на обслуживании. Попробуйте чуть позже.")

        plugin = self.find(command)

        if plugin is None:
            if any(command in known.commands for known in self._plugins):
                raise CommandError("Эта команда временно отключена.")

            raise CommandError(f"Unknown command: {command}")

        if plugin.web_only and identity.kind != WEB:
            # Refused here rather than inside the plugin: the transport is the
            # router's business, and a plugin carrying that check would have to
            # know it had two of them.
            raise CommandError(
                f"{command} is available on the web console only.",
            )

        result = await plugin.execute(command, args, identity)
        plugin.changed(identity)
        return result

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
        # text parser. Order matters: aiogram matches in registration order,
        # and a handler on the parent router fires before its sub-routers — so
        # the catch-all text parser must also be a sub-router, and the last
        # one, or it would swallow "/start" and every button label first.
        router.include_router(build_menu_router(self.execute))

        for plugin in self._plugins:
            plugin_router = plugin.router()

            if plugin_router.message.handlers or plugin_router.callback_query.handlers:
                router.include_router(plugin_router)

        commands_router = Router(name="commands")

        @commands_router.message()
        async def dispatch(message: Message) -> None:
            if message.document is not None:
                try:
                    result = await self.receive_file(message)
                except CommandError as error:
                    await message.answer(str(error))
                    return

                await send_result(message, result)
                return

            text = message.text or message.caption

            if not text:
                return

            urls = [] if text.lstrip().startswith("/") else _urls(message)

            if urls:
                try:
                    result = await self.receive_links(message, urls)
                except CommandError as error:
                    await message.answer(str(error))
                    return

                await send_result(message, result)
                return

            if not message.text:
                return

            try:
                command, args = parse_command(message.text)
                identity = telegram(message.chat.id)
                result = await self.execute(command, args, identity)
            except CommandError as error:
                await message.answer(str(error))
                return

            await send_result(message, result)

        router.include_router(commands_router)

        return router

    def _receiver(self, flag: str, refusal: str) -> BasePlugin:
        if self.switches and self.switches.maintenance():
            raise CommandError("Сервис на обслуживании. Попробуйте чуть позже.")

        plugin = next((one for one in self._enabled() if getattr(one, flag)), None)

        if plugin is None:
            raise CommandError(refusal)

        return plugin

    async def receive_file(self, message: Message) -> CommandResult:
        plugin = self._receiver("accepts_files", "Файлы сейчас не принимаются.")

        document = message.document
        limit = plugin.max_file_bytes

        if document.file_size is None or document.file_size > limit:
            raise CommandError(f"Файл больше {limit // 1000} КБ.")

        buffer = BytesIO()
        await message.bot.download(document, destination=buffer)
        data = buffer.getvalue()

        if len(data) > limit:
            raise CommandError(f"Файл больше {limit // 1000} КБ.")

        identity = telegram(message.chat.id)
        result = await plugin.receive_file(identity, document.file_name or "", data)
        plugin.changed(identity)
        return result

    async def receive_links(self, message: Message, urls: list[str]) -> CommandResult:
        return await self.save_links(
            telegram(message.chat.id), urls, message.text or message.caption or ""
        )

    async def save_links(
        self, identity: Identity, urls: list[str], text: str
    ) -> CommandResult:
        plugin = self._receiver("accepts_links", "Ссылки сейчас не принимаются.")

        result = await plugin.receive_links(identity, urls, text)
        plugin.changed(identity)
        return result

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


TELEGRAM_LIMIT = 4096


def _urls(message: Message) -> list[str]:
    text = message.text or message.caption or ""
    urls = []

    for entity in message.entities or message.caption_entities or []:
        if entity.type == "url":
            urls.append(entity.extract_from(text))
        elif entity.type == "text_link" and entity.url:
            urls.append(entity.url)

    return urls


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def send_result(message: Message, result: CommandResult) -> None:
    if result.file is not None:
        name, data = result.file
        await message.answer_document(
            BufferedInputFile(data, filename=name),
            caption=result.text[:1000] or None,
        )
        return

    if not result.text:
        return

    if result.preformatted and _utf16(result.text) <= TELEGRAM_LIMIT:
        await message.answer(
            result.text,
            entities=[
                MessageEntity(type="pre", offset=0, length=_utf16(result.text))
            ],
        )
        return

    await message.answer(result.text[:TELEGRAM_LIMIT])
