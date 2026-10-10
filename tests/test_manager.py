import logging

import pytest

from app.core.identity import telegram
from app.core.results import CommandError
from app.plugins.clipboard import ClipboardPlugin
from app.plugins.manager import PluginManager
from app.plugins.notes import NotesPlugin


@pytest.fixture
def manager(database_with_schema):
    mgr = PluginManager(logging.getLogger("test"))
    mgr.register(NotesPlugin(database_with_schema))
    mgr.register(ClipboardPlugin(database_with_schema))
    return mgr


class TestFind:
    def test_finds_declared_command(self, manager):
        assert manager.find("/add").name == "notes"

    def test_returns_none_for_unknown(self, manager):
        assert manager.find("/nope") is None

    def test_registered_order_decides_ownership(self, database_with_schema):
        mgr = PluginManager(logging.getLogger("test"))
        first = NotesPlugin(database_with_schema)
        second = ClipboardPlugin(database_with_schema)

        mgr.register(first)
        mgr.register(second)
        second.commands = {**second.commands, "/add": "Shadowed"}

        assert mgr.find("/add") is first


class TestExecute:
    async def test_dispatches_to_owning_plugin(self, manager, web_identity):
        result = await manager.execute("/add", "milk", web_identity)

        assert result.text == "Saved"

    async def test_unknown_command_raises(self, manager, web_identity):
        with pytest.raises(CommandError, match="Unknown command"):
            await manager.execute("/missing", "", web_identity)

    async def test_error_message_comes_from_plugin(self, manager, web_identity):
        with pytest.raises(CommandError, match="Usage"):
            await manager.execute("/add", "", web_identity)


class TestWebOnly:
    """A plugin that only makes sense on a web page is refused in a chat.

    The check belongs to the router: a plugin carrying it would have to know
    it had two transports to keep straight.
    """

    async def test_a_web_only_plugin_is_reached_from_the_web(
        self, expenses_manager, web_identity
    ):
        result = await expenses_manager.execute(
            "/spend", "", web_identity
        )

        assert "web console" in result.text

    async def test_the_same_plugin_is_refused_in_a_chat(self, expenses_manager):
        with pytest.raises(CommandError, match="web console only"):
            await expenses_manager.execute("/spend", "", telegram(4242))


class TestPages:
    def test_a_page_is_found_by_its_path(self, expenses_manager):
        plugin = expenses_manager.find_by_page("/expenses")

        assert plugin is not None
        assert plugin.name == "expenses"

    def test_an_unclaimed_path_has_no_owner(self, expenses_manager):
        assert expenses_manager.find_by_page("/nothing") is None

    def test_only_plugins_with_a_page_are_listed(self, expenses_manager):
        pages = expenses_manager.pages()

        assert [page["page"] for page in pages] == ["/expenses"]

    def test_a_manager_without_pages_lists_nothing(self, manager):
        assert manager.pages() == []


class TestGetPlugins:
    def test_returns_a_copy(self, manager):
        plugins = manager.get_plugins()
        plugins.clear()

        assert len(manager.get_plugins()) == 2

    def test_preserves_registration_order(self, manager):
        assert [p.name for p in manager.get_plugins()] == ["notes", "clipboard"]


class TestBuildRouter:
    def test_routes_commands_even_without_plugin_routers(self, manager):
        router = manager.build_router()

        assert router.name == "platform"
        assert len(router.message.handlers) == 1

    def test_includes_plugin_specific_router_when_present(self, manager):
        from aiogram import Router
        from aiogram.types import Message

        plugin = ClipboardPlugin()
        extra = Router(name="extra")

        @extra.message(lambda m: m.text == "/custom")
        async def custom(message: Message):
            await message.answer("custom")

        plugin.router = lambda: extra
        manager.register(plugin)

        router = manager.build_router()

        assert len(router.sub_routers) == 1
        assert router.sub_routers[0].name == "extra"

    def test_skips_empty_plugin_routers(self, manager):
        assert len(manager.build_router().sub_routers) == 0


class TestTelegramDispatch:
    """The router must convert a Telegram message into an identity."""

    def test_router_dispatch_is_wired(self, manager):
        router = manager.build_router()
        handler = router.message.handlers[0].callback

        assert callable(handler)
