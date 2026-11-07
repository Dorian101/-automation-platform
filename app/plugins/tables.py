import functools
import importlib
import pkgutil

import app.plugins
from app.plugins.base import BasePlugin


@functools.cache
def _plugins() -> tuple[type[BasePlugin], ...]:
    for module in pkgutil.iter_modules(app.plugins.__path__):
        importlib.import_module(f"{app.plugins.__name__}.{module.name}")
    return tuple(BasePlugin.__subclasses__())


def data_tables() -> list[tuple[str, str]]:
    return [item for plugin in _plugins() for item in plugin.tables.items()]


def demo_tables() -> list[str]:
    return [table for plugin in _plugins() if plugin.demo for table in plugin.tables]
