import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.identity import Identity, is_guest
from app.core.results import CommandError, CommandResult
from app.db.collections_repo import CollectionsRepository
from app.db.database import Database
from app.db.settings_repo import SettingsRepository

from .base import BasePlugin

MAX_FIELDS = 20
MAX_KEPT_FIELDS = 100
MAX_RECORDS = 2000
GUEST_RECORD_LIMIT = 50

DEMO_FIELDS = [
    {"key": "f1", "label": "Название", "type": "text", "required": True},
    {"key": "f2", "label": "Автор", "type": "text", "required": False},
    {
        "key": "f3",
        "label": "Статус",
        "type": "select",
        "required": False,
        "options": ["хочу", "читаю", "прочитал"],
    },
    {"key": "f4", "label": "Оценка", "type": "rating", "required": False},
    {"key": "f5", "label": "Прочитана", "type": "date", "required": False},
]
DEMO_BOOKS = (
    {"f1": "Мастер и Маргарита", "f2": "Михаил Булгаков", "f3": "прочитал",
     "f4": 5, "f5": "2026-09-14"},
    {"f1": "Дюна", "f2": "Фрэнк Герберт", "f3": "прочитал", "f4": 4,
     "f5": "2026-10-02"},
    {"f1": "Думай медленно… решай быстро", "f2": "Даниэль Канеман",
     "f3": "читаю"},
    {"f1": "Storytelling with Data", "f2": "Cole Nussbaumer Knaflic",
     "f3": "читаю"},
    {"f1": "Пикник на обочине", "f2": "Аркадий и Борис Стругацкие",
     "f3": "хочу"},
)
MAX_TITLE = 60
MAX_LABEL = 40
MAX_TEXT = 500
MAX_LONG = 5000
MAX_URL = 2000
MAX_OPTIONS = 20
MAX_OPTION = 40
MAX_NUMBER = Decimal("1e12")
SHOWN_COLUMNS = 6

TYPES = {
    "text": "Текст",
    "textarea": "Длинный текст",
    "number": "Число",
    "money": "Сумма",
    "date": "Дата",
    "bool": "Да/нет",
    "select": "Выбор",
    "url": "Ссылка",
    "rating": "Оценка 1–5",
}
YES_NO = [{"value": "1", "label": "Да"}, {"value": "0", "label": "Нет"}]
RATINGS = [{"value": str(n), "label": "★" * n} for n in range(1, 6)]


def _clean(raw, limit: int, what: str, multiline: bool = False) -> str:
    text = raw if isinstance(raw, str) else ""
    allowed = "\n\t" if multiline else ""
    text = "".join(
        char
        for char in text
        if char in allowed or not unicodedata.category(char).startswith("C")
    ).strip()

    if len(text) > limit:
        raise CommandError(f"{what}: не длиннее {limit} символов.")

    return text


def _number(raw: str, label: str, places: int | None) -> str:
    try:
        number = Decimal(raw.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise CommandError(f"{label}: нужно число.") from None

    if not number.is_finite() or abs(number) >= MAX_NUMBER:
        raise CommandError(f"{label}: нужно число.")

    if places is not None:
        number = number.quantize(Decimal(1).scaleb(-places))

    return format(number if places is not None else number.normalize(), "f")


def value_of(field: dict, raw) -> object:
    kind, label = field["type"], field["label"]
    long = kind == "textarea"
    text = _clean(raw, MAX_LONG if long else MAX_URL, label, multiline=long)

    if not text:
        if field.get("required"):
            raise CommandError(f"Заполните поле «{label}».")
        return None

    if kind == "text" and len(text) > MAX_TEXT:
        raise CommandError(f"{label}: не длиннее {MAX_TEXT} символов.")

    if kind == "number":
        return _number(text, label, None)

    if kind == "money":
        return _number(text, label, 2)

    if kind == "date":
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise CommandError(f"{label}: нужна дата.") from None

    if kind == "bool":
        if text not in ("1", "0"):
            raise CommandError(f"{label}: да или нет.")
        return text == "1"

    if kind == "select":
        if text not in field.get("options", []):
            raise CommandError(f"{label}: выберите вариант из списка.")
        return text

    if kind == "url":
        if not text.startswith(("https://", "http://")) or any(
            char.isspace() for char in text
        ):
            raise CommandError(f"{label}: нужна ссылка, начинающаяся с https://.")
        return text

    if kind == "rating":
        if text not in ("1", "2", "3", "4", "5"):
            raise CommandError(f"{label}: оценка от 1 до 5.")
        return int(text)

    return text


def shown(field: dict, value) -> str:
    if value is None or value == "":
        return ""

    try:
        return _shown(field["type"], value)
    except (TypeError, ValueError, ArithmeticError):
        return str(value)[:80]


def _shown(kind: str, value) -> str:
    if kind == "bool":
        return "Да" if value else "Нет"

    if kind == "rating":
        return "★" * int(value)

    if kind == "date":
        try:
            return date.fromisoformat(value).strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            return str(value)

    if kind == "money":
        whole, _, cents = str(value).partition(".")
        sign = "−" if Decimal(str(value)) < 0 else ""
        digits = f"{int(whole.lstrip('-') or 0):,}".replace(",", " ")
        return f"{sign}{digits},{(cents or '00')[:2].ljust(2, '0')}"

    text = str(value)
    return text if len(text) <= 80 else text[:79] + "…"


def _id(payload: dict, key: str, missing: str) -> int:
    value = payload.get(key, "")

    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or len(value) > 18
    ):
        raise CommandError(missing)

    return int(value)


def _form_value(field: dict, value) -> str:
    if value is None:
        return ""
    if field["type"] == "bool":
        return "1" if value else "0"
    return str(value)


def _input(field: dict, value=None) -> dict:
    kind = field["type"]
    spec = {
        "name": field["key"],
        "label": field["label"],
        "required": field.get("required", False),
    }

    if kind in ("select", "bool", "rating"):
        options = {
            "select": [
                {"value": one, "label": one} for one in field.get("options", [])
            ],
            "bool": YES_NO,
            "rating": RATINGS,
        }[kind]
        options = [{"value": "", "label": "—"}, *options]
        spec.update(type="select", options=options)
    else:
        spec["type"] = {
            "textarea": "textarea",
            "date": "date",
            "url": "url",
        }.get(kind, "text")

    if value is not None:
        spec["value"] = _form_value(field, value)

    return spec


class CollectionsPlugin(BasePlugin):
    name = "collections"
    tables = {"collections": "Коллекции", "collection_records": "Записи коллекций"}
    version = "0.1.0"
    description = "Свои коллекции: задайте поля, и платформа сделает страницу"
    page = "/collections"
    page_title = "Коллекции"
    demo = True
    commands = {}

    def __init__(self, database: Database | None = None, persons=None, settings=None):
        self.repo = CollectionsRepository(database)
        self._settings = settings or SettingsRepository(database)
        self._persons = persons

    async def execute(
        self, command: str, args: str, identity: Identity
    ) -> CommandResult:
        raise CommandError(f"Unknown command: {command}")

    async def page_view(self, identity: Identity) -> dict:
        return _for(identity, self._home(identity))

    async def page_action(self, identity: Identity, action: str, payload: dict) -> dict:
        return _for(identity, await self._act(identity, action, payload))

    async def seed_demo(self, identity: Identity) -> None:
        collection_id = self.repo.create(identity, [identity], "Книги", 1)
        if collection_id is None:
            return
        self.repo.update([identity], collection_id, "Книги", DEMO_FIELDS)
        for book in reversed(DEMO_BOOKS):
            self.repo.add_record(identity, collection_id, book, GUEST_RECORD_LIMIT)

    async def _act(self, identity: Identity, action: str, payload: dict) -> dict:
        readable = self._readable_as(identity)

        if action == "create":
            title = _clean(payload.get("title"), MAX_TITLE, "Название")
            if not title:
                raise CommandError("Назовите коллекцию.")
            created = self.repo.create(
                identity, readable, title, self._settings.collections_per_user()
            )
            if created is None:
                raise CommandError("Достигнут предел числа коллекций.")
            return self._fields_screen(identity, self._collection(readable, created))

        if action == "remove":
            if not self.repo.delete(
                readable, _id(payload, "id", "Коллекция не найдена.")
            ):
                raise CommandError("Коллекция не найдена.")
            return self._home(identity)

        if action in ("view", "home") and not payload.get("collection"):
            return self._home(identity)

        collection = self._collection(
            readable, _id(payload, "collection", "Коллекция не найдена.")
        )

        if action == "home":
            return self._home(identity)

        if action == "fields":
            return self._fields_screen(identity, collection)

        if action == "rename":
            title = _clean(payload.get("title"), MAX_TITLE, "Название")
            if not title:
                raise CommandError("Назовите коллекцию.")
            self.repo.update(readable, collection["id"], title, collection["fields"])
            return self._fields_screen(
                identity, self._collection(readable, collection["id"])
            )

        if action == "addfield":
            fields = [*collection["fields"], _new_field(collection["fields"], payload)]
            self.repo.update(readable, collection["id"], collection["title"], fields)
            return self._fields_screen(
                identity, self._collection(readable, collection["id"])
            )

        if action == "dropfield":
            key = payload.get("key", "")
            if not any(one["key"] == key for one in _active(collection)):
                raise CommandError("Поле не найдено.")
            fields = [
                {**one, "dropped": True} if one["key"] == key else one
                for one in collection["fields"]
            ]
            self.repo.update(readable, collection["id"], collection["title"], fields)
            return self._fields_screen(
                identity, self._collection(readable, collection["id"])
            )

        if action == "add":
            if not _active(collection):
                raise CommandError("Сначала добавьте поля.")
            limit = GUEST_RECORD_LIMIT if is_guest(identity) else MAX_RECORDS
            if not self.repo.add_record(
                identity, collection["id"], _record(collection, payload), limit
            ):
                raise CommandError(f"В коллекции уже {limit} записей.")
            return self._collection_screen(collection)

        if action == "edit":
            record = self._record(collection, payload)
            return self._collection_screen(collection, editing=record)

        if action == "update":
            record = self._record(collection, payload)
            if not self.repo.update_record(
                collection["id"], record["id"], _record(collection, payload)
            ):
                raise CommandError("Запись не найдена.")
            return self._collection_screen(collection)

        if action == "delete":
            if not self.repo.delete_record(
                collection["id"], _id(payload, "record", "Запись не найдена.")
            ):
                raise CommandError("Запись не найдена.")
            return self._collection_screen(collection)

        if action == "view" and payload.get("screen") == "fields":
            return self._fields_screen(identity, collection)

        if action in ("view", "open"):
            return self._collection_screen(collection)

        raise CommandError(f"Неизвестное действие: {action}")

    def _collection(self, readable, collection_id: int) -> dict:
        collection = self.repo.get(readable, collection_id)

        if collection is None:
            raise CommandError("Коллекция не найдена.")

        return collection

    def _record(self, collection: dict, payload: dict) -> dict:
        record = self.repo.record(
            collection["id"], _id(payload, "record", "Запись не найдена.")
        )

        if record is None:
            raise CommandError("Запись не найдена.")

        return record

    def _home(self, identity: Identity) -> dict:
        owned = self.repo.owned(self._readable_as(identity))
        limit = self._settings.collections_per_user()
        view = {
            "title": "Коллекции",
            "live": True,
            "layout": ["forms", "list"],
            "keep": {"collection": "", "screen": ""},
            "forms": [],
            "list": {
                "title": "Мои коллекции",
                "empty": "Пока нет ни одной. Назовите первую выше.",
                "columns": [
                    {"key": "title", "label": "Название"},
                    {"key": "fields", "label": "Полей", "num": True},
                    {"key": "count", "label": "Записей", "num": True},
                ],
                "rows": [
                    {
                        "title": one["title"],
                        "fields": len(_active(one)),
                        "count": one["count"],
                        "actions": [
                            {
                                "action": "open",
                                "label": "Открыть",
                                "kind": "ghost",
                                "params": {"collection": str(one["id"])},
                            },
                            {
                                "action": "remove",
                                "label": "Удалить",
                                "kind": "ghost",
                                "params": {"id": str(one["id"])},
                            },
                        ],
                    }
                    for one in owned
                ],
                "actions": "actions",
            },
        }

        if len(owned) < limit:
            view["forms"].append(
                {
                    "id": "create",
                    "title": f"Новая коллекция ({len(owned)} из {limit})",
                    "submit": "Создать",
                    "fields": [
                        {
                            "name": "title",
                            "label": "Название",
                            "type": "text",
                            "required": True,
                            "placeholder": "Например, Книги",
                        }
                    ],
                }
            )
        else:
            view["notice"] = f"Создано {len(owned)} из {limit} коллекций."

        return view

    def _tools(self, collection: dict, fields_screen: bool) -> list[dict]:
        params = {"collection": str(collection["id"])}
        return [
            {
                "action": "home",
                "label": "← Все коллекции",
                "params": {"collection": ""},
            },
            {
                "action": "open" if fields_screen else "fields",
                "label": "К записям" if fields_screen else "Поля",
                "params": params,
            },
        ]

    def _collection_screen(self, collection: dict, editing: dict | None = None) -> dict:
        fields = _active(collection)
        shown_fields = fields[:SHOWN_COLUMNS]
        rows = []

        for record in self.repo.records(collection["id"]):
            params = {"record": str(record["id"])}
            row = {
                field["key"]: shown(field, record["data"].get(field["key"]))
                for field in shown_fields
            }
            row["actions"] = [
                {
                    "action": "edit",
                    "label": "Изменить",
                    "kind": "ghost",
                    "params": params,
                },
                {
                    "action": "delete",
                    "label": "Удалить",
                    "kind": "ghost",
                    "params": params,
                },
            ]
            rows.append(row)

        view = {
            "title": collection["title"],
            "live": True,
            "layout": ["tools", "forms", "list"],
            "keep": {"collection": str(collection["id"]), "screen": ""},
            "tools": self._tools(collection, False),
            "forms": [],
            "list": {
                "title": f"«{collection['title']}» · записей: {len(rows)}",
                "empty": "Записей пока нет.",
                "columns": [
                    {"key": field["key"], "label": field["label"]}
                    for field in shown_fields
                ],
                "rows": rows,
                "actions": "actions",
            },
        }

        if not fields:
            view["notice"] = "У коллекции нет полей. Откройте «Поля» и добавьте их."
        elif editing is not None:
            view["forms"].append(
                {
                    "id": "update",
                    "title": "Изменить запись",
                    "submit": "Сохранить",
                    "focus": True,
                    "fields": [
                        *(
                            _input(field, editing["data"].get(field["key"]))
                            for field in fields
                        ),
                        {
                            "name": "record",
                            "type": "hidden",
                            "value": str(editing["id"]),
                        },
                    ],
                    "cancel": {"label": "Отмена", "action": "view"},
                }
            )
        else:
            view["forms"].append(
                {
                    "id": "add",
                    "title": "Новая запись",
                    "submit": "Добавить",
                    "fields": [_input(field) for field in fields],
                }
            )

        return view

    def _fields_screen(self, identity: Identity, collection: dict) -> dict:
        fields = _active(collection)
        view = {
            "title": f"{collection['title']}: поля",
            "layout": ["tools", "forms", "list"],
            "keep": {"collection": str(collection["id"]), "screen": "fields"},
            "tools": self._tools(collection, True),
            "forms": [
                {
                    "id": "rename",
                    "title": "Название коллекции",
                    "submit": "Переименовать",
                    "fields": [
                        {
                            "name": "title",
                            "label": "Название",
                            "type": "text",
                            "required": True,
                            "value": collection["title"],
                        }
                    ],
                }
            ],
            "list": {
                "title": f"Поля «{collection['title']}»: {len(fields)} из {MAX_FIELDS}",
                "empty": "Полей пока нет.",
                "columns": [
                    {"key": "label", "label": "Поле"},
                    {"key": "type", "label": "Тип"},
                    {"key": "required", "label": "Обязательное"},
                    {"key": "options", "label": "Варианты"},
                ],
                "rows": [
                    {
                        "label": field["label"],
                        "type": TYPES.get(field["type"], field["type"]),
                        "required": "да" if field.get("required") else "",
                        "options": ", ".join(field.get("options", [])),
                        "actions": [
                            {
                                "action": "dropfield",
                                "label": "Удалить",
                                "kind": "ghost",
                                "params": {"key": field["key"]},
                            }
                        ],
                    }
                    for field in fields
                ],
                "actions": "actions",
            },
        }

        if len(fields) < MAX_FIELDS:
            view["forms"].append(
                {
                    "id": "addfield",
                    "title": "Новое поле",
                    "submit": "Добавить поле",
                    "fields": [
                        {
                            "name": "label",
                            "label": "Название",
                            "type": "text",
                            "required": True,
                        },
                        {
                            "name": "type",
                            "label": "Тип",
                            "type": "select",
                            "options": [
                                {"value": key, "label": label}
                                for key, label in TYPES.items()
                            ],
                        },
                        {
                            "name": "required",
                            "label": "Обязательное",
                            "type": "select",
                            "options": [
                                {"value": "0", "label": "Нет"},
                                {"value": "1", "label": "Да"},
                            ],
                        },
                        {
                            "name": "options",
                            "label": "Варианты для «Выбора», через запятую",
                            "type": "text",
                        },
                    ],
                }
            )

        return view


def _active(collection: dict) -> list[dict]:
    return [field for field in collection["fields"] if not field.get("dropped")]


def _for(identity: Identity, view: dict) -> dict:
    if is_guest(identity):
        view["live"] = False
    return view


def _new_field(every: list[dict], payload: dict) -> dict:
    fields = [field for field in every if not field.get("dropped")]
    if len(fields) >= MAX_FIELDS or len(every) >= MAX_KEPT_FIELDS:
        raise CommandError(f"Не больше {MAX_FIELDS} полей.")

    label = _clean(payload.get("label"), MAX_LABEL, "Название поля")
    if not label:
        raise CommandError("Назовите поле.")
    if any(one["label"].casefold() == label.casefold() for one in fields):
        raise CommandError("Поле с таким названием уже есть.")

    kind = payload.get("type", "")
    if not isinstance(kind, str) or kind not in TYPES:
        raise CommandError("Неизвестный тип поля.")

    field = {
        "key": f"f{max((int(one['key'][1:]) for one in every), default=0) + 1}",
        "label": label,
        "type": kind,
        "required": payload.get("required") == "1",
    }

    if kind == "select":
        options = []
        for raw in _clean(
            payload.get("options"), MAX_OPTIONS * (MAX_OPTION + 1), "Варианты"
        ).split(","):
            option = raw.strip()
            if option and option not in options:
                if len(option) > MAX_OPTION:
                    raise CommandError(f"Вариант не длиннее {MAX_OPTION} символов.")
                options.append(option)
        if not options:
            raise CommandError("Для «Выбора» перечислите варианты через запятую.")
        if len(options) > MAX_OPTIONS:
            raise CommandError(f"Не больше {MAX_OPTIONS} вариантов.")
        field["options"] = options

    return field


def _record(collection: dict, payload: dict) -> dict:
    data = {}

    for field in _active(collection):
        value = value_of(field, payload.get(field["key"], ""))
        if value is not None:
            data[field["key"]] = value

    if not data:
        raise CommandError("Заполните хотя бы одно поле.")

    return data
