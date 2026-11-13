import pytest

from app.core.identity import WEB, Identity
from app.core.results import CommandError
from app.db.settings_repo import COLLECTIONS_PER_USER, SettingsRepository
from app.plugins import collections as module
from app.plugins.collections import CollectionsPlugin, shown, value_of

OTHER = Identity(kind=WEB, id="someone-else")


@pytest.fixture
def settings(database_with_schema, monkeypatch):
    monkeypatch.setattr("app.db.settings_repo.CACHE_SECONDS", 0)
    return SettingsRepository(database_with_schema)


@pytest.fixture
def plugin(database_with_schema, settings):
    return CollectionsPlugin(database_with_schema, settings=settings)


async def _books(plugin, identity):
    view = await plugin.page_action(identity, "create", {"title": "Книги"})
    collection = view["keep"]["collection"]
    for label, kind, extra in (
        ("Название", "text", {"required": "1"}),
        ("Статус", "select", {"options": "хочу, читаю, прочитал"}),
        ("Оценка", "rating", {}),
        ("Цена", "money", {}),
    ):
        view = await plugin.page_action(
            identity,
            "addfield",
            {"collection": collection, "label": label, "type": kind, **extra},
        )
    return collection, view


class TestValues:
    @pytest.mark.parametrize(
        ("kind", "raw", "stored"),
        [
            ("number", "1 234,5", "1234.5"),
            ("money", "99.999", "100.00"),
            ("date", "2026-11-13", "2026-11-13"),
            ("bool", "1", True),
            ("rating", "4", 4),
            ("url", "https://example.com/a", "https://example.com/a"),
            ("textarea", "строка\nещё", "строка\nещё"),
        ],
    )
    def test_a_value_is_stored_by_its_type(self, kind, raw, stored):
        assert value_of({"type": kind, "label": "x"}, raw) == stored

    @pytest.mark.parametrize(
        ("kind", "raw"),
        [
            ("number", "abc"),
            ("number", "Infinity"),
            ("date", "13.11.2026"),
            ("bool", "maybe"),
            ("rating", "6"),
            ("url", "javascript:alert(1)"),
            ("text", "x" * (module.MAX_TEXT + 1)),
        ],
    )
    def test_a_wrong_value_is_refused(self, kind, raw):
        with pytest.raises(CommandError):
            value_of({"type": kind, "label": "x"}, raw)

    def test_a_choice_must_be_one_of_the_options(self):
        field = {"type": "select", "label": "x", "options": ["a", "b"]}

        assert value_of(field, "b") == "b"
        with pytest.raises(CommandError):
            value_of(field, "c")

    def test_a_required_field_must_be_filled(self):
        with pytest.raises(CommandError, match="Заполните"):
            value_of({"type": "text", "label": "x", "required": True}, "  ")

    def test_values_are_shown_for_people(self):
        assert shown({"type": "money"}, "1234567.50") == "1 234 567,50"
        assert shown({"type": "rating"}, 3) == "★★★"
        assert shown({"type": "date"}, "2026-11-13") == "13.11.2026"
        assert shown({"type": "bool"}, False) == "Нет"


class TestCollections:
    async def test_a_collection_is_built_from_its_fields(self, plugin, web_identity):
        collection, view = await _books(plugin, web_identity)

        assert view["keep"]["screen"] == "fields"
        assert [row["label"] for row in view["list"]["rows"]] == [
            "Название", "Статус", "Оценка", "Цена"
        ]

        view = await plugin.page_action(
            web_identity, "open", {"collection": collection}
        )
        form = view["forms"][0]
        status = next(f for f in form["fields"] if f["label"] == "Статус")

        assert form["id"] == "add"
        values = [o["value"] for o in status["options"]]
        assert values == ["", "хочу", "читаю", "прочитал"]

    async def test_a_record_is_added_edited_and_deleted(self, plugin, web_identity):
        collection, _ = await _books(plugin, web_identity)
        payload = {"collection": collection, "f1": "Дюна", "f2": "читаю", "f4": "450"}

        view = await plugin.page_action(web_identity, "add", payload)
        row = view["list"]["rows"][0]

        assert row["f1"] == "Дюна"
        assert row["f4"] == "450,00"

        record = row["actions"][0]["params"]["record"]
        view = await plugin.page_action(
            web_identity, "edit", {"collection": collection, "record": record}
        )
        assert view["forms"][0]["id"] == "update"

        view = await plugin.page_action(
            web_identity,
            "update",
            {**payload, "record": record, "f2": "прочитал", "f3": "5"},
        )
        assert view["list"]["rows"][0]["f3"] == "★★★★★"

        view = await plugin.page_action(
            web_identity, "delete", {"collection": collection, "record": record}
        )
        assert view["list"]["rows"] == []

    async def test_a_refresh_keeps_the_fields_screen(self, plugin, web_identity):
        collection, view = await _books(plugin, web_identity)

        again = await plugin.page_action(web_identity, "view", view["keep"])

        assert again["keep"] == {"collection": collection, "screen": "fields"}

    async def test_a_dropped_field_keeps_its_values_out_of_sight(
        self, plugin, web_identity
    ):
        collection, _ = await _books(plugin, web_identity)
        await plugin.page_action(
            web_identity, "add", {"collection": collection, "f1": "Дюна", "f4": "1"}
        )

        await plugin.page_action(
            web_identity, "dropfield", {"collection": collection, "key": "f4"}
        )
        view = await plugin.page_action(
            web_identity, "open", {"collection": collection}
        )

        assert [c["key"] for c in view["list"]["columns"]] == ["f1", "f2", "f3"]
        assert plugin.repo.records(int(collection))[0]["data"]["f4"] == "1.00"


class TestLimits:
    async def test_the_number_of_collections_follows_the_setting(
        self, plugin, settings, web_identity
    ):
        settings.put(COLLECTIONS_PER_USER, "2")
        await plugin.page_action(web_identity, "create", {"title": "Один"})
        view = await plugin.page_action(web_identity, "create", {"title": "Два"})

        with pytest.raises(CommandError, match="предел"):
            await plugin.page_action(web_identity, "create", {"title": "Три"})

        home = await plugin.page_view(web_identity)
        assert home["forms"] == []
        assert view["title"] == "Два: поля"

    async def test_fields_are_capped(self, plugin, web_identity, monkeypatch):
        monkeypatch.setattr(module, "MAX_FIELDS", 1)
        view = await plugin.page_action(web_identity, "create", {"title": "А"})
        collection = view["keep"]["collection"]
        await plugin.page_action(
            web_identity, "addfield",
            {"collection": collection, "label": "x", "type": "text"},
        )

        with pytest.raises(CommandError, match="Не больше"):
            await plugin.page_action(
                web_identity, "addfield",
                {"collection": collection, "label": "y", "type": "text"},
            )

    async def test_records_are_capped(self, plugin, web_identity, monkeypatch):
        monkeypatch.setattr(module, "MAX_RECORDS", 1)
        collection, _ = await _books(plugin, web_identity)
        await plugin.page_action(
            web_identity, "add", {"collection": collection, "f1": "a"}
        )

        with pytest.raises(CommandError, match="записей"):
            await plugin.page_action(
                web_identity, "add", {"collection": collection, "f1": "b"}
            )

    async def test_an_unknown_field_type_is_refused(self, plugin, web_identity):
        view = await plugin.page_action(web_identity, "create", {"title": "А"})

        with pytest.raises(CommandError, match="тип"):
            await plugin.page_action(
                web_identity, "addfield",
                {"collection": view["keep"]["collection"], "label": "x", "type": "sql"},
            )


class TestOwnership:
    async def test_someone_elses_collection_is_not_found(self, plugin, web_identity):
        collection, _ = await _books(plugin, web_identity)

        for action, extra in (
            ("open", {}),
            ("add", {"f1": "x"}),
            ("addfield", {"label": "z", "type": "text"}),
            ("rename", {"title": "Моё"}),
        ):
            with pytest.raises(CommandError, match="не найдена"):
                await plugin.page_action(
                    OTHER, action, {"collection": collection, **extra}
                )

        with pytest.raises(CommandError, match="не найдена"):
            await plugin.page_action(OTHER, "remove", {"id": collection})

        assert (await plugin.page_view(OTHER))["list"]["rows"] == []

    async def test_a_record_of_another_collection_cannot_be_reached(
        self, plugin, web_identity
    ):
        mine, _ = await _books(plugin, web_identity)
        await plugin.page_action(OTHER, "create", {"title": "Чужая"})
        theirs = (await plugin.page_view(OTHER))["list"]["rows"][0]
        theirs_id = theirs["actions"][0]["params"]["collection"]
        await plugin.page_action(
            OTHER, "addfield", {"collection": theirs_id, "label": "a", "type": "text"}
        )
        view = await plugin.page_action(
            OTHER, "add", {"collection": theirs_id, "f1": "секрет"}
        )
        record = view["list"]["rows"][0]["actions"][0]["params"]["record"]

        with pytest.raises(CommandError, match="Запись не найдена"):
            await plugin.page_action(
                web_identity, "edit", {"collection": mine, "record": record}
            )
        with pytest.raises(CommandError, match="Запись не найдена"):
            await plugin.page_action(
                web_identity, "delete", {"collection": mine, "record": record}
            )


class TestReviewFindings:
    async def test_a_new_field_never_takes_a_dropped_fields_key(
        self, plugin, web_identity
    ):
        collection, _ = await _books(plugin, web_identity)
        await plugin.page_action(
            web_identity, "add", {"collection": collection, "f1": "a", "f4": "5"}
        )
        await plugin.page_action(
            web_identity, "dropfield", {"collection": collection, "key": "f4"}
        )

        view = await plugin.page_action(
            web_identity,
            "addfield",
            {"collection": collection, "label": "Звёзды", "type": "rating"},
        )
        records = await plugin.page_action(
            web_identity, "open", {"collection": collection}
        )

        assert view["list"]["rows"][-1]["actions"][0]["params"]["key"] == "f5"
        assert records["list"]["rows"][0]["f5"] == ""

    def test_a_stored_value_of_another_type_is_shown_as_text(self):
        assert shown({"type": "rating"}, "abc") == "abc"
        assert shown({"type": "money"}, "abc") == "abc"

    async def test_a_malformed_type_is_refused_not_crashed(self, plugin, web_identity):
        view = await plugin.page_action(web_identity, "create", {"title": "А"})

        with pytest.raises(CommandError, match="тип"):
            await plugin.page_action(
                web_identity,
                "addfield",
                {"collection": view["keep"]["collection"], "label": "x", "type": []},
            )

    @pytest.mark.parametrize("raw", ["²", "9" * 19, "-1"])
    async def test_a_strange_id_is_not_found(self, plugin, web_identity, raw):
        with pytest.raises(CommandError, match="не найдена"):
            await plugin.page_action(web_identity, "open", {"collection": raw})

    def test_a_number_is_kept_in_plain_notation(self):
        assert value_of({"type": "number", "label": "x"}, "1e5") == "100000"
        assert shown({"type": "money"}, "-0.00") == "0,00"

    async def test_every_choice_starts_empty(self, plugin, web_identity):
        collection, _ = await _books(plugin, web_identity)
        await plugin.page_action(
            web_identity,
            "addfield",
            {"collection": collection, "label": "Да?", "type": "bool", "required": "1"},
        )

        view = await plugin.page_action(
            web_identity, "open", {"collection": collection}
        )
        choice = view["forms"][0]["fields"][-1]

        assert choice["options"][0]["value"] == ""
