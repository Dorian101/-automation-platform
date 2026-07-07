from abc import ABC, abstractmethod
from aiogram import Router


class BasePlugin(ABC):
    name = "base"
    version = "0.1.0"
    description = "Base plugin"
    
    commands: dict[str, str] = {}

    async def on_startup(self) -> None:
        """Called when the platform starts."""
        pass

    async def on_shutdown(self) -> None:
        """Called before the platform stops."""
        pass

    @abstractmethod
    def router(self) -> Router:
        """Return plugin router."""
        raise NotImplementedError