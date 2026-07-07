from abc import ABC, abstractmethod
from aiogram import Router


class BasePlugin(ABC):
    name = "base"
    version = "0.1.0"
    description = "Base plugin"
    
    commands: dict[str, str] = {}

    @abstractmethod
    def router(self) -> Router:
        """Return plugin router."""
        raise NotImplementedError