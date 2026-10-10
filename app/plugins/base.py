from abc import ABC, abstractmethod

from aiogram import Router

from app.core.identity import Identity
from app.core.results import CommandError, CommandResult


class BasePlugin(ABC):
    name: str = "base"
    version: str = "0.1.0"
    description: str = "Base plugin"
    commands: dict[str, str] = {}

    # The web path this plugin owns, or None when it has no page. The platform
    # routes it automatically, the same way it routes a declared command, so a
    # plugin with a page is not an adapter the web layer has to know about.
    page: str | None = None

    # Plugins that are only worth using through the web console set this and
    # leave commands alone: a plugin that returns a payload for the analytics
    # screen has nothing to say in a chat message, and the router refuses
    # rather than the plugin checking the transport itself.
    web_only: bool = False

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

    async def page_view(self, identity: Identity) -> dict:
        """Describe this plugin's page, and the data already in it.

        A description rather than markup: the web layer paints what it is
        told, so a plugin can add a field without the platform growing a
        branch for it. The keys are the ones :mod:`app.web.pages` renders.

        Args:
            identity: Who is asking. The same one a command would receive.

        Returns:
            A page description. The default is an empty one, for a plugin that
            has a page declared but nothing to put on it yet.
        """
        return {}

    async def page_action(
        self,
        identity: Identity,
        action: str,
        payload: dict,
    ) -> dict:
        """Handle something the page did and return what to redraw with.

        Args:
            identity: Who is asking.
            action: What was done, e.g. ``"spend"`` or ``"catadd"``.
            payload: What the page sent. Every value is a string; the plugin
                converts and validates, because a page is not a trusted caller.

        Returns:
            A page description, the same shape ``page_view`` returns, with
            whatever the action changed already applied.

        Raises:
            CommandError: If the action cannot be performed. The message is
                shown to the user as-is, so it must be safe to display.
        """
        raise CommandError(f"{self.name}: unknown action: {action!r}")
