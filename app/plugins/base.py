from abc import ABC, abstractmethod

from aiogram import Router

from app.core.identity import Identity
from app.core.results import CommandResult


class BasePlugin(ABC):
    name: str = "base"
    version: str = "0.1.0"
    description: str = "Base plugin"
    commands: dict[str, str] = {}

    @abstractmethod
    async def execute(
        self,
        command: str,
        args: str,
        identity: Identity,
    ) -> CommandResult:
        """Run a command and return a transport-agnostic result.

        Args:
            command: The command including its leading slash, e.g. ``"/add"``.
            args: Everything after the command, already stripped.
            identity: Who is asking. Carries the transport it came through.

        Raises:
            CommandError: If the input is invalid. The message is shown to the
                user as-is, so it must be safe to display.
        """
        raise NotImplementedError

    async def on_startup(self) -> None:
        """Called when the platform starts."""
        return None

    async def on_shutdown(self) -> None:
        """Called before the platform stops."""
        return None

    def router(self) -> Router:
        """Return transport-specific handlers owned by this plugin.

        The platform routes declared commands automatically, so a plugin only
        overrides this when it needs to react to something a command cannot
        express, such as inline keyboards or file uploads.
        """
        return Router(name=f"plugin:{self.name}")
