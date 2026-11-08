import functools
import json
import re
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
DEFAULT = "ru"
LANGUAGES = ("ru", "en")
RELEASED = ("ru",)

_KEY = re.compile(r"\{\{([a-z0-9_.]+)\}\}")


@functools.cache
def strings(lang: str) -> dict:
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def prefix(lang: str) -> str:
    return "" if lang == DEFAULT else f"/{lang}"


def split(path: str) -> tuple[str, str]:
    for lang in LANGUAGES:
        if lang != DEFAULT and path.startswith(f"/{lang}/"):
            return lang, path[len(lang) + 1:]
    return DEFAULT, path


def localize(source: str, lang: str) -> str:
    table = strings(lang)
    return _KEY.sub(lambda match: table[match.group(1)], source).replace(
        "__PREFIX__", prefix(lang)
    )


def script_strings(lang: str) -> str:
    body = json.dumps(strings(lang)["js"], ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="i18n">{body}</script>'


def plural(lang: str, number: int, forms: str) -> str:
    one, few, many = forms.split("|")

    if lang != "ru":
        return one if number == 1 else many

    if 11 <= number % 100 <= 14:
        return many

    return {1: one, 2: few, 3: few, 4: few}.get(number % 10, many)
