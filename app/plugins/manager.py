from typing import List
from aiogram import Dispatcher

from .base import BasePlugin


class PluginManager:
    def __init__(self, logger):
        self._plugins: List[BasePlugin] = []
        self.logger = logger

    def register(self, plugin: BasePlugin):
        self._plugins.append(plugin)
        
        self.logger.info(
            "Registered plugin: %s",
            plugin.name
        )

    def setup(self, dp: Dispatcher):
        for plugin in self._plugins:
            dp.include_router(plugin.router())
            
    def get_plugins(self):
        return self._plugins.copy()
    
    async def startup(self):
        for plugin in self._plugins:
            await plugin.on_startup()
            
            self.logger.info(
                "Started plugin: %s",
                plugin.name
            )


    async def shutdown(self):
        for plugin in reversed(self._plugins):
            await plugin.on_shutdown()
            
            self.logger.info(
                "Stopped plugin: %s",
                plugin.name
            )