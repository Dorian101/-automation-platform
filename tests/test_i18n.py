import re
from datetime import date

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.web import i18n
from app.web.server import TEMPLATES_DIR, _experience

BASE_URL = "https://public.example"
CYRILLIC = re.compile("[А-Яа-яЁё]")


@pytest.fixture
async def crawler(pages_manager, database_with_schema, monkeypatch, web_server_factory):
    monkeypatch.setattr("app.web.server.PUBLIC_BASE_URL", BASE_URL)
    server = web_server_factory(pages_manager, database_with_schema)
    async with TestClient(TestServer(server.build_app())) as client:
        yield client


def test_every_language_has_the_same_keys():
    keys = {lang: set(i18n.strings(lang)) for lang in i18n.LANGUAGES}
    js = {lang: set(i18n.strings(lang)["js"]) for lang in i18n.LANGUAGES}

    for lang in i18n.LANGUAGES:
        assert keys[lang] == keys[i18n.DEFAULT], lang
        assert js[lang] == js[i18n.DEFAULT], lang


@pytest.mark.parametrize("template", ["about.html", "project.html"])
def test_every_key_a_template_uses_exists(template):
    source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    used = set(re.findall(r"\{\{([a-z0-9_.]+)\}\}", source))

    assert used
    assert used <= set(i18n.strings(i18n.DEFAULT))


@pytest.fixture
async def admin(crawler, login, create_user, users):
    from tests.conftest import TEST_USERNAME

    users.set_admin(users.get_by_username(TEST_USERNAME).id, True)
    await login(crawler)
    return crawler


@pytest.mark.parametrize("path", ["/about", "/project"])
async def test_each_page_answers_in_every_language_to_an_admin(admin, path):
    for lang in i18n.LANGUAGES:
        response = await admin.get(i18n.prefix(lang) + path)
        body = await response.text()

        assert response.status == 200
        assert f'<html lang="{lang}">' in body
        assert "{{" not in body and "__PREFIX__" not in body


@pytest.mark.parametrize("path", ["/en/about", "/en/project"])
async def test_the_english_pages_have_no_russian_text(admin, path):
    body = await (await admin.get(path)).text()
    visible = re.sub(r"<style.*?</style>|<script.*?</script>", "", body, flags=re.S)

    assert CYRILLIC.findall(visible) == []
    assert '"theme_light": "Light theme"' in body


async def test_an_admin_sees_the_switch_both_ways(admin):
    russian = await (await admin.get("/about")).text()
    english = await (await admin.get("/en/about")).text()

    assert 'class="lang-switch" href="/en/about"' in russian
    assert 'class="lang-switch" href="/about"' in english
    assert 'href="/en/project"' in english


@pytest.mark.parametrize("path", ["/en/about", "/en/project"])
async def test_an_unreleased_language_is_hidden_from_everyone_else(crawler, path):
    assert "en" not in i18n.RELEASED
    assert (await crawler.get(path)).status == 404

    russian = await (await crawler.get(path[3:])).text()
    assert 'class="lang-switch"' not in russian
    assert "hreflang" not in russian

    sitemap = await (await crawler.get("/sitemap.xml")).text()
    robots = await (await crawler.get("/robots.txt")).text()
    assert "/en/" not in sitemap and "/en/" not in robots


async def test_the_live_trace_answers_in_the_page_language(crawler):
    trace = await (await crawler.get("/project/trace?scenario=expenses&lang=en")).json()

    assert trace["reply"] == "The expenses page was built from the database"


def test_experience_reads_in_english():
    assert _experience(date(2026, 11, 7), "en") == "3 years 5 months"
    assert _experience(date(2024, 7, 1), "en") == "1 year 1 month"
