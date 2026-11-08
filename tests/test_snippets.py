import logging
from types import SimpleNamespace

import pytest

from app.core.identity import WEB, Identity, telegram
from app.core.results import CommandError, CommandResult
from app.plugins import snippets as module
from app.plugins.manager import PluginManager, send_result
from app.plugins.snippets import MAX_BYTES, SnippetsPlugin, _filename

OTHER = Identity(kind=WEB, id="someone-else")
SQL = "SELECT a || ' | ' || b\r\n\tFROM t -- **не** ~~спойлер~~ ||x||\n"


@pytest.fixture
def plugin(database_with_schema):
    return SnippetsPlugin(database_with_schema)


async def _save(plugin, identity, body, title=""):
    return await plugin.page_action(identity, "save", {"title": title, "body": body})


async def _open(plugin, identity, view, row=0):
    params = view["list"]["rows"][row]["actions"][1]["params"]
    return await plugin.page_action(identity, "open", params)


class TestText:
    async def test_the_text_comes_back_byte_for_byte(self, plugin, web_identity):
        view = await _save(plugin, web_identity, SQL)

        opened = await _open(plugin, web_identity, view)

        assert opened["panel"]["text"] == SQL
        assert view["list"]["rows"][0]["title"] == "SELECT a || ' | ' || b"

    async def test_control_characters_leave_the_title(self, plugin, web_identity):
        view = await _save(plugin, web_identity, "x", title="a\x1b[31mb‮c")

        assert view["list"]["rows"][0]["title"] == "a[31mbc"

    @pytest.mark.parametrize(
        "body, message",
        [
            ("a\x00b", "нулевой байт"),
            ("   \n\t", "Пустой"),
            ("x" * (MAX_BYTES + 1), "больше"),
            ("\ud800", "недопустимые"),
            (["list"], "строкой"),
        ],
    )
    async def test_bad_text_is_refused(self, plugin, web_identity, body, message):
        with pytest.raises(CommandError, match=message):
            await _save(plugin, web_identity, body)

        assert plugin.repo.entries(web_identity) == []

    async def test_only_the_newest_hundred_stay(
        self, plugin, web_identity, monkeypatch
    ):
        monkeypatch.setattr("app.db.snippets_repo.KEEP", 3)
        for number in range(5):
            await _save(plugin, web_identity, f"text {number}")

        titles = [entry["title"] for entry in plugin.repo.entries(web_identity)]
        assert titles == ["text 4", "text 3", "text 2"]


class TestAccess:
    async def test_another_account_cannot_open_or_delete(self, plugin, web_identity):
        view = await _save(plugin, web_identity, "secret")
        params = view["list"]["rows"][0]["actions"][1]["params"]

        with pytest.raises(CommandError, match="не найден"):
            await plugin.page_action(OTHER, "open", params)
        with pytest.raises(CommandError, match="не найден"):
            await plugin.page_action(OTHER, "delete", params)

        assert (await plugin.execute("/snippets", "", OTHER)).text.startswith(
            "Сниппетов нет"
        )

    async def test_a_forged_id_is_refused(self, plugin, web_identity):
        with pytest.raises(CommandError, match="не найден"):
            await plugin.page_action(web_identity, "open", {"id": "1 OR 1=1"})


class TestFiles:
    async def test_a_utf8_file_is_saved_without_its_bom(self, plugin):
        owner = telegram(42)

        result = await plugin.receive_file(
            owner, "../../etc/report.sql", b"\xef\xbb\xbf" + SQL.encode()
        )

        [entry] = plugin.repo.entries(owner)
        assert entry["title"] == "report.sql"
        assert plugin.repo.get(owner, entry["id"])["body"] == SQL
        assert "report.sql" in result.text

    @pytest.mark.parametrize(
        "data", [b"\x89PNG\r\n\x1a\n\x00\x00", "привет".encode("cp1251")]
    )
    async def test_a_binary_or_foreign_file_is_refused(self, plugin, data):
        with pytest.raises(CommandError, match="не текстовый"):
            await plugin.receive_file(telegram(42), "x.bin", data)

    def test_the_file_name_is_made_safe(self):
        assert _filename("query.SQL") == "query.sql"
        assert _filename("../../etc/passwd") == "etc_passwd.txt"
        assert _filename("отчёт: итог?") == "отчёт_итог.txt"
        assert _filename("...") == "snippet.txt"


class TestBot:
    async def test_a_short_one_comes_as_a_code_block(self, plugin):
        owner = telegram(42)
        await plugin.receive_file(owner, "a.sql", SQL.encode())

        result = await plugin.execute("/snippet_get", "1", owner)

        assert result.preformatted and result.text == SQL

    async def test_a_long_one_comes_as_a_file(self, plugin, monkeypatch):
        monkeypatch.setattr(module, "INLINE_LIMIT", 10)
        owner = telegram(42)
        await plugin.receive_file(owner, "big.sql", SQL.encode())

        result = await plugin.execute("/snippet_get", "1", owner)

        assert result.file == ("big.sql", SQL.encode())

    @pytest.mark.parametrize("args", ["", "0", "5", "-1", "1; DROP TABLE x"])
    async def test_a_bad_number_is_refused(self, plugin, args):
        with pytest.raises(CommandError):
            await plugin.execute("/snippet_get", args, telegram(42))

    async def test_the_list_is_numbered_newest_first(self, plugin):
        owner = telegram(42)
        await plugin.receive_file(owner, "old.txt", b"a")
        await plugin.receive_file(owner, "new.txt", b"b")

        text = (await plugin.execute("/snippets", "", owner)).text

        assert text.index("1. new.txt") < text.index("2. old.txt")


class FakeMessage:
    def __init__(self, size, data=b"x"):
        self.chat = SimpleNamespace(id=42)
        self.document = SimpleNamespace(file_size=size, file_name="a.txt")
        self.downloaded = False
        self.sent = []
        self._data = data
        self.bot = SimpleNamespace(download=self._download)

    async def _download(self, document, destination):
        self.downloaded = True
        destination.write(self._data)

    async def answer(self, text, entities=None):
        self.sent.append((text, entities))


class TestTransport:
    @pytest.fixture
    def manager(self, plugin):
        manager = PluginManager(logging.getLogger("test"))
        manager.register(plugin)
        return manager

    async def test_an_oversized_file_is_not_downloaded(self, manager):
        message = FakeMessage(MAX_BYTES + 1)

        with pytest.raises(CommandError, match="больше"):
            await manager.receive_file(message)

        assert message.downloaded is False

    async def test_a_size_lie_is_caught_after_download(self, manager):
        message = FakeMessage(10, data=b"x" * (MAX_BYTES + 1))

        with pytest.raises(CommandError, match="больше"):
            await manager.receive_file(message)

    async def test_a_file_of_unknown_size_is_not_downloaded(self, manager):
        message = FakeMessage(None)

        with pytest.raises(CommandError, match="больше"):
            await manager.receive_file(message)

        assert message.downloaded is False

    @pytest.mark.parametrize(
        "switches, reply",
        [
            ((set(), True), "обслуживании"),
            (({"snippets"}, False), "не принимаются"),
        ],
    )
    async def test_a_switched_off_service_refuses_before_download(
        self, manager, switches, reply
    ):
        disabled, maintenance = switches
        manager.switches = SimpleNamespace(
            disabled_plugins=lambda: disabled, maintenance=lambda: maintenance
        )
        message = FakeMessage(10)

        with pytest.raises(CommandError, match=reply):
            await manager.receive_file(message)

        assert message.downloaded is False

    async def test_a_file_lands_in_the_senders_snippets(self, manager, plugin):
        message = FakeMessage(len(SQL.encode()), data=SQL.encode())

        await manager.receive_file(message)

        [entry] = plugin.repo.entries(telegram(42))
        assert plugin.repo.get(telegram(42), entry["id"])["body"] == SQL

    async def test_the_code_block_covers_the_whole_text(self):
        message = FakeMessage(1)
        text = "😀 a || b"

        await send_result(message, CommandResult(text=text, preformatted=True))

        [(sent, entities)] = message.sent
        assert sent == text
        assert (entities[0].type, entities[0].offset, entities[0].length) == (
            "pre",
            0,
            len(text) + 1,
        )


class TestLive:
    async def test_the_page_refreshes_itself(self, plugin, web_identity):
        assert (await plugin.page_view(web_identity))["live"] is True

    async def test_an_answered_action_stays_live(self, plugin, web_identity):
        view = await _save(plugin, web_identity, SQL)

        assert view["live"] is True


class TestCopyAndEdit:
    async def test_copying_from_the_list_opens_nothing(self, plugin, web_identity):
        view = await _save(plugin, web_identity, SQL)
        copy = view["list"]["rows"][0]["actions"][0]

        result = await plugin.page_action(web_identity, copy["action"], copy["params"])

        assert copy["copy"] is True
        assert result["clip"] == SQL
        assert "panel" not in result

    async def test_an_opened_snippet_is_edited_in_place(self, plugin, web_identity):
        view = await _save(plugin, web_identity, "old text")
        opened = await _open(plugin, web_identity, view)
        save = opened["panel"]["save"]

        result = await plugin.page_action(
            web_identity,
            save["action"],
            {**save["params"], "title": "Новое", "body": SQL},
        )

        assert result["notice"] == "Изменения сохранены."
        assert result["panel"]["text"] == SQL
        assert [row["title"] for row in result["list"]["rows"]] == ["Новое"]

    async def test_an_edit_is_checked_like_a_new_one(self, plugin, web_identity):
        view = await _save(plugin, web_identity, "text")
        save = (await _open(plugin, web_identity, view))["panel"]["save"]

        with pytest.raises(CommandError, match="нулевой"):
            await plugin.page_action(
                web_identity, save["action"], {**save["params"], "body": "a\x00"}
            )

    async def test_another_account_cannot_edit(self, plugin, web_identity):
        view = await _save(plugin, web_identity, "mine")
        save = (await _open(plugin, web_identity, view))["panel"]["save"]

        with pytest.raises(CommandError, match="не найден"):
            await plugin.page_action(
                OTHER, save["action"], {**save["params"], "body": "theirs"}
            )
