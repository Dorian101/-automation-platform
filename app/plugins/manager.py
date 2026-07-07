from typing import List
from aiogram import Dispatcher

from .base import BasePlugin


class PluginManager:
    def __init__(self):
        self._plugins: List[BasePlugin] = []

    def register(self, plugin: BasePlugin):
        self._plugins.append(plugin)

    def setup(self, dp: Dispatcher):
        for plugin in self._plugins:
            dp.include_router(plugin.router())
            
    def get_plugins(self):
        return self._plugins.copy()