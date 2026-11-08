import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from aiogram.types import Chat, Message, MessageEntity

from app.core.identity import WEB, Identity, telegram
from app.core.results import CommandError
from app.plugins import bookmarks as module
from app.plugins.bookmarks import BookmarksPlugin, domain, extract_title, normalize
from app.plugins.manager import PluginManager, _urls

OTHER = Identity(kind=WEB, id="someone-else")


async def _title(url):
    return f"Title of {url}"


@pytest.fixture
def plugin(database_with_schema):
    return BookmarksPlugin(database_with_schema, fetch=_title)


async def _settle(plugin):
    await asyncio.gather(*list(plugin._pending))


async def _save(plugin, identity, url, description=""):
    return await plugin.page_action(
        identity, "save", {"url": url, "description": description}
    )


def _message(text, entities=None, caption=False):
    fields = (
        {"caption": text, "caption_entities": entities}
        if caption
        else {
            "text": text,
            "entities": entities,
        }
    )
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=42, type="private"),
        **fields,
    )


class TestNormalize:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (
                "HTTPS://Example.COM/a/?utm_source=x&id=5#top",
                "https://example.com/a?id=5#top",
            ),
            ("https://example.com/v?print&UTM_MEDIUM=x", "https://example.com/v?print"),
            ("https://example.com/s?q=a%2Fb+c", "https://example.com/s?q=a%2Fb+c"),
            ("https://example.com/#/item/5", "https://example.com#/item/5"),
            ("example.com", "https://example.com"),
            ("http://example.com:80/", "http://example.com"),
            ("https://example.com:8443/x", "https://example.com:8443/x"),
            ("https://t.me/c/1?fbclid=1&ysclid=2", "https://t.me/c/1"),
        ],
    )
    def test_one_page_has_one_spelling(self, raw, expected):
        assert normalize(raw) == expected

    def test_an_international_host_is_stored_in_ascii(self):
        parts = urlsplit(normalize("https://пример.рф/путь"))

        assert (parts.hostname, parts.path) == ("xn--e1afmkfd.xn--p1ai", "/путь")
        assert domain(normalize("пример.рф")) == "пример.рф"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "just words",
            "ftp://example.com",
            "javascript:alert(1)",
            "https://localhost",
        ],
    )
    def test_not_a_link_is_refused(self, raw):
        with pytest.raises(CommandError):
            normalize(raw)


class TestTitle:
    def test_og_title_wins_over_title(self):
        body = (
            b"<html><head><title>Site | Page</title>"
            b'<meta property="og:title" content="Page &amp; more"></head>'
        )
        assert extract_title(body, "utf-8") == "Page & more"

    def test_title_is_decoded_by_the_meta_charset(self):
        body = '<meta charset="windows-1251"><title> Привет\n мир </title>'.encode(
            "cp1251"
        )
        assert extract_title(body, None) == "Привет мир"

    def test_a_page_without_title_gives_nothing(self):
        assert extract_title(b"<html></html>", "utf-8") == ""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://198.18.0.0/x",
            "http://[::1]/",
            "file:///etc/passwd",
            "http://1.1.1.1:8080/",
            "https://8.8.8.8:5432/",
            "http://１２７。０.0.1/",
            "http://[64:ff9b::7f00:1]/",
            "http://[::ffff:127.0.0.1]/",
        ],
    )
    async def test_private_addresses_and_odd_ports_are_not_fetched(self, url):
        assert await module.fetch_title(url) == ""

    async def test_a_name_resolving_to_a_private_address_is_refused(self, monkeypatch):
        async def private(self, host, port=0, family=0):
            return [
                {
                    "hostname": host,
                    "host": "192.0.2.0",
                    "port": port,
                    "family": family,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        monkeypatch.setattr(module.ThreadedResolver, "resolve", private)

        with pytest.raises(OSError):
            await module._PublicResolver().resolve("intranet.example", 80)


class TestSaving:
    async def test_the_title_arrives_after_saving(self, plugin, web_identity):
        view = await _save(plugin, web_identity, "https://example.com/a")

        assert view["list"]["rows"][0]["title"] == "example.com"

        await _settle(plugin)
        view = await plugin.page_view(web_identity)

        assert view["list"]["rows"][0]["title"] == "Title of https://example.com/a"

    async def test_a_failed_fetch_leaves_the_domain(
        self, database_with_schema, web_identity
    ):
        async def broken(url):
            raise TimeoutError

        plugin = BookmarksPlugin(database_with_schema, fetch=broken)
        await _save(plugin, web_identity, "www.example.com/a")
        await _settle(plugin)

        [row] = (await plugin.page_view(web_identity))["list"]["rows"]
        assert row["title"] == "example.com"

    async def test_a_repeat_rises_instead_of_doubling(self, plugin, web_identity):
        await _save(plugin, web_identity, "https://example.com/a")
        await _save(plugin, web_identity, "https://example.com/b")
        first = plugin.repo.entries(web_identity)[1]
        plugin.repo.mark(web_identity, first["id"], True)

        view = await _save(
            plugin, web_identity, "HTTPS://EXAMPLE.com/a/?utm_medium=tg", "again"
        )

        rows = view["list"]["rows"]
        assert len(rows) == 2
        assert rows[0]["url"] == "https://example.com/a"
        assert rows[0]["description"] == "again"
        assert rows[0]["state"] == ""
        assert "уже была" in view["notice"]

    async def test_a_repeat_without_text_keeps_the_description(
        self, plugin, web_identity
    ):
        await _save(plugin, web_identity, "https://example.com", "first")
        await _save(plugin, web_identity, "https://example.com")

        assert plugin.repo.entries(web_identity)[0]["description"] == "first"

    async def test_a_linked_pair_shares_one_copy(
        self, database_with_schema, web_identity
    ):
        pair = (web_identity, telegram(42))
        plugin = BookmarksPlugin(
            database_with_schema,
            persons=SimpleNamespace(identities=lambda identity: pair),
            fetch=_title,
        )

        await _save(plugin, web_identity, "https://example.com")
        result = await plugin.receive_links(
            telegram(42), ["example.com"], "example.com"
        )

        assert result.text.startswith("Уже было")
        assert len(plugin.repo.entries(pair)) == 1

    async def test_people_do_not_see_each_others_links(self, plugin, web_identity):
        view = await _save(plugin, web_identity, "https://example.com")
        params = view["list"]["rows"][0]["actions"][2]["params"]

        assert (await plugin.page_view(OTHER))["list"]["rows"] == []
        with pytest.raises(CommandError, match="не найдена"):
            await plugin.page_action(OTHER, "delete", params)


class TestPage:
    async def test_read_edit_delete(self, plugin, web_identity):
        view = await _save(plugin, web_identity, "https://example.com")
        params = view["list"]["rows"][0]["actions"][0]["params"]

        view = await plugin.page_action(web_identity, "read", params)
        assert view["list"]["rows"][0]["state"] == "прочитано"
        assert view["list"]["rows"][0]["actions"][0]["action"] == "unread"

        view = await plugin.page_action(web_identity, "edit", params)
        assert view["forms"][0]["id"] == "update"

        view = await plugin.page_action(
            web_identity, "update", {**params, "title": "Мой", "description": "зачем"}
        )
        row = view["list"]["rows"][0]
        assert (row["title"], row["description"]) == ("Мой", "зачем")
        assert view["forms"][0]["id"] == "save"

        view = await plugin.page_action(web_identity, "delete", params)
        assert view["list"]["rows"] == []

    async def test_an_edited_title_is_not_overwritten_by_the_fetch(
        self, plugin, web_identity
    ):
        view = await _save(plugin, web_identity, "https://example.com")
        params = view["list"]["rows"][0]["actions"][0]["params"]
        await plugin.page_action(
            web_identity, "update", {**params, "title": "Мой", "description": ""}
        )
        await _settle(plugin)

        assert plugin.repo.entries(web_identity)[0]["title"] == "Мой"


class TestBot:
    @pytest.fixture
    def manager(self, plugin):
        manager = PluginManager(logging.getLogger("test"))
        manager.register(plugin)
        return manager

    def test_links_are_found_in_entities(self):
        text = "Почитать 😀 example.com/a и ещё"
        offset = len("Почитать 😀 ".encode("utf-16-le")) // 2
        message = _message(
            text,
            [
                MessageEntity(type="url", offset=offset, length=13),
                MessageEntity(
                    type="text_link", offset=0, length=8, url="https://t.me/x"
                ),
            ],
        )

        assert _urls(message) == ["example.com/a", "https://t.me/x"]

    def test_a_caption_counts(self):
        message = _message(
            "https://example.com",
            [MessageEntity(type="url", offset=0, length=19)],
            caption=True,
        )

        assert _urls(message) == ["https://example.com"]

    async def test_text_around_the_link_becomes_the_description(self, manager, plugin):
        text = "https://example.com/a — про индексы"
        message = _message(text, [MessageEntity(type="url", offset=0, length=21)])

        result = await manager.receive_links(message, _urls(message))

        assert result.text == "Сохранено: example.com"
        [entry] = plugin.repo.entries(telegram(42))
        assert entry["description"] == "— про индексы"
        assert entry["source"] == "telegram"

    async def test_a_switched_off_plugin_refuses(self, manager):
        manager.switches = SimpleNamespace(
            disabled_plugins=lambda: {"bookmarks"}, maintenance=lambda: False
        )
        message = _message(
            "https://example.com", [MessageEntity(type="url", offset=0, length=19)]
        )

        with pytest.raises(CommandError, match="не принимаются"):
            await manager.receive_links(message, _urls(message))

    async def test_the_list_command(self, plugin):
        await plugin.receive_links(telegram(42), ["https://example.com"], "")
        await _settle(plugin)

        text = (await plugin.execute("/links", "", telegram(42))).text

        assert text == "1. Title of https://example.com\nhttps://example.com"
