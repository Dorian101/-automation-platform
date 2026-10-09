from app.core.identity import WEB, Identity
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

    def __init__(self, manager: PluginManager, users=None, links=None):
        self.manager = manager
        # Both optional: /plugins and /help must work without a database, and
        # only /status asks where a notification would land.
        self._users = users
        self._links = links

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

        lines.extend(["", f"Notifications: {self._delivery(identity)}"])

        return reply("\n".join(lines))

    def _delivery(self, identity: Identity) -> str:
        """Where a reminder from this identity would actually arrive.

        A reminder that quietly goes nowhere is the failure this surfaces: the
        plugin works, the command succeeds, and nothing is ever received.
        """
        if identity.kind != WEB:
            return "Telegram"

        if self._links is None:
            return "web (no Telegram link available)"

        user = self._users.get_by_username(identity.id)

        if user is None:
            return "web (no such account)"

        link = self._links.get_by_user_id(user.id)

        if link is None:
            return "nowhere — link your Telegram account in the console"

        return f"Telegram chat {link.telegram_id}"
