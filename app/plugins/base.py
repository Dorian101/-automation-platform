from abc import ABC, abstractmethod
from aiogram import Router


class BasePlugin(ABC):
    name: str

    @abstractmethod
    def router(self) -> Router:
        pass