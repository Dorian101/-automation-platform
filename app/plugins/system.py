from aiogram import Router
from aiogram.types import Message

from .base import BasePlugin
from .manager import PluginManager


class SystemPlugin(BasePlugin):
    name = "system"
    version = "1.0.0"
    description = "System commands"
    commands = {
    "/plugins": "Show loaded plugins",
    "/help": "Show available commands",
    "/status": "Show platform status"
    }

    def __init__(self, manager: PluginManager):
        self.manager = manager

    def router(self) -> Router:
        router = Router()

        @router.message(lambda m: m.text == "/plugins")
        async def plugins(message: Message):
            lines = ["Active plugins:"]

            for plugin in self.manager.get_plugins():
                lines.append(
                    f"- {plugin.name} {plugin.version} — {plugin.description}"
                )

            await message.answer("\n".join(lines))
            
        @router.message(lambda m: m.text == "/help")
        async def help_command(message: Message):
            lines = ["Available commands:"]

            for plugin in self.manager.get_plugins():
                lines.append(f"\n[{plugin.name}]")

                for command, description in plugin.commands.items():
                    lines.append(f"{command} — {description}")

            await message.answer("\n".join(lines))
        
        @router.message(lambda m: m.text == "/status")
        async def status(message: Message):
            plugins = self.manager.get_plugins()

            lines = [
                "Automation Platform",
                "",
                f"Plugins loaded: {len(plugins)}",
                "",
                "Active plugins:",
            ]

            for plugin in plugins:
                lines.append(f"✓ {plugin.name}")

            await message.answer("\n".join(lines))
        
        return router