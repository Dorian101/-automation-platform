import logging

import pytest
from aiohttp.test_utils import TestServer

from app import manage
from app.core.config import Config
from app.db.links_repo import TelegramLinksRepository
from app.db.users_repo import UsersRepository
from app.main import build_manager
from app.notifications import Notifier
from app.plugins.base import BasePlugin
from app.plugins.expenses import ExpensesPlugin
from app.plugins.tables import _plugins


def _shipped() -> list[type[BasePlugin]]:
    return [
        plugin for plugin in _plugins() if plugin.__module__.startswith("app.plugins.")
    ]


def _manager(database):
    return build_manager(
        database,
        Notifier([]),
        UsersRepository(database),
        TelegramLinksRepository(database),
        logging.getLogger("test"),
    )


class TestEveryPluginIsChecked:
    def test_every_plugin_is_registered(self, database_with_schema):
        manager = _manager(database_with_schema)
        registered = {type(plugin) for plugin in manager.get_plugins()}

        assert set(_shipped()) == registered

    @pytest.mark.parametrize("plugin", _shipped(), ids=lambda plugin: plugin.__name__)
    def test_every_plugin_has_a_check(self, plugin):
        if plugin.page:
            return

        assert plugin.check in plugin.commands
        assert plugin.check not in plugin.command_args


@pytest.fixture
async def running(database_with_schema, web_server_factory, monkeypatch):
    server = web_server_factory(_manager(database_with_schema), database_with_schema)
    test_server = TestServer(server.build_app())
    await test_server.start_server()
    monkeypatch.setattr(Config, "WEB_HOST", test_server.host)
    monkeypatch.setattr(Config, "WEB_PORT", test_server.port)
    yield
    await test_server.close()


class TestVerify:
    async def test_a_healthy_service_passes(self, running, capsys):
        assert await manage._verify() == 0

        out = capsys.readouterr().out
        assert "health: ok" in out
        assert "trace reminder: ok" in out
        assert "plugin todo: ok" in out

    async def test_a_broken_plugin_fails_the_deploy(self, running, monkeypatch, capsys):
        async def broken(self, identity):
            raise RuntimeError("relation does not exist")

        monkeypatch.setattr(ExpensesPlugin, "page_view", broken)

        assert await manage._verify() == 1
        assert "failed: trace expenses, plugin expenses" in capsys.readouterr().out

    async def test_a_service_that_never_answers_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(Config, "WEB_PORT", 9)
        monkeypatch.setattr(manage, "VERIFY_WAIT_SECONDS", 0)

        assert await manage._verify() == 1
        assert "health: not ok" in capsys.readouterr().out
