from app.core.identity import Identity
from app.core.results import CommandResult, reply

from .base import BasePlugin
from .manager import PluginManager


class SystemPlugin(BasePlugin):
    name = "system"
    version = "1.1.0"
    description = "System commands"
    commands = {
        "/plugins": "Show loaded plugins",
        "/help": "Show available commands",
        "/status": "Show platform status",
    }

    def __init__(self, manager: PluginManager):
        self.manager = manager

    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        if command == "/plugins":
            lines = ["Active plugins:"]

            for plugin in self.manager.get_plugins():
                lines.append(
                    f"- {plugin.name} {plugin.version} — {plugin.description}",
                )

            return reply("\n".join(lines))

        if command == "/help":
            lines = ["Available commands:"]

            for plugin in self.manager.get_plugins():
                lines.append(f"\n[{plugin.name}]")

                for name, description in plugin.commands.items():
                    lines.append(f"{name} — {description}")

            return reply("\n".join(lines))

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

        return reply("\n".join(lines))
