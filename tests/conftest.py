# ruff: noqa: E402

import logging
import os
import re

# Config reads the environment exactly once, when its class body runs at import
# time. Setting these inside a fixture would happen after every test module had
# already imported app.core.config, so the values would be ignored and the tests
# would quietly run against the development database, emptying it. They must be
# in place before the first application import below.
os.environ["DB_HOST"] = "localhost"
os.environ["DB_PORT"] = "5432"
os.environ["DB_NAME"] = "automation_platform_test"
os.environ["DB_USER"] = "automation"
os.environ["DB_PASSWORD"] = ""

# TestClient talks plain http, and a Secure cookie would be stored but never
# sent back, so every login would look successful and every session would be
# missing. The production default is the opposite; test_web checks that.
os.environ["SESSION_COOKIE_SECURE"] = "false"

import pytest

from app.core.config import Config
from app.core.identity import WEB, Identity
from app.db.database import Database
from app.db.migrations import MigrationRunner
from app.db.users_repo import UsersRepository
from app.notifications import Notifier, TelegramChannel, WebChannel
from app.plugins.clipboard import ClipboardPlugin
from app.plugins.expenses import ExpensesPlugin
from app.plugins.manager import PluginManager
from app.plugins.notes import NotesPlugin
from app.web.auth import AUTH_HEADER

TEST_USERNAME = "alexey"
TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture
def database():
    db = Database()
    _clean_db(db)
    yield db
    _clean_db(db)


@pytest.fixture
def database_with_schema():
    db = Database()
    _clean_db(db)
    MigrationRunner(db).run()
    yield db
    _clean_db(db)


def _clean_db(db: Database):
    """Drop every table in the public schema.

    Listing tables explicitly is what let stale ones survive: a table created
    by a later migration was not in the list, stayed behind, and the next
    migration run failed on it. Dropping the whole schema contents keeps this
    correct as the schema grows.
    """
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DO $$
                DECLARE
                    tbl TEXT;
                BEGIN
                    FOR tbl IN SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public'
                    LOOP
                        EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', tbl);
                    END LOOP;
                END
                $$
            """)
        conn.commit()


@pytest.fixture
def manager(database_with_schema):
    mgr = PluginManager(logging.getLogger("test"))
    mgr.register(NotesPlugin(database_with_schema))
    mgr.register(ClipboardPlugin(database_with_schema))
    return mgr


@pytest.fixture
def expenses_manager(manager, database_with_schema):
    """A manager that also has the spending plugin, which owns a page."""
    manager.register(ExpensesPlugin(database_with_schema))
    return manager


@pytest.fixture
def users(database_with_schema):
    return UsersRepository(database_with_schema)


@pytest.fixture
def create_user(users):
    """Ensure the default account exists and return a factory for extra ones.

    Tests that sign in only have to depend on this; nobody has to remember to
    call it before the first login attempt.
    """
    def _create(username=TEST_USERNAME, password=TEST_PASSWORD):
        return users.create(username, password)

    _create()

    return _create


@pytest.fixture
def login():
    """Return a function that signs a test client in through the real form.

    It returns the login response, so tests asserting on the status or the
    cookie can use it directly while the rest just await it for the side
    effect.
    """
    return _login


async def _login(
    client,
    username=TEST_USERNAME,
    password=TEST_PASSWORD,
    expect_status: int = 303,
):
    """Submit the login form the way a browser behind the proxy would.

    The transport header is added when a token is configured, exactly as the
    reverse proxy does in production. Tests that disable the token get no
    header, which is the point of disabling it.
    """
    headers = (
        {AUTH_HEADER: Config.WEB_ACCESS_TOKEN} if Config.WEB_ACCESS_TOKEN else {}
    )

    # A real browser would already be sitting on the form, holding its token
    # in a hidden field and in a cookie. Fetching the page is what gives the
    # test client exactly that state.
    page = await client.get("/login", headers=headers, allow_redirects=False)

    response = await client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf": await _hidden_field(page, "csrf"),
        },
        headers=headers,
        allow_redirects=False,
    )

    assert response.status == expect_status, await response.text()

    return response


async def _hidden_field(response, name: str) -> str:
    """Read a hidden input's value out of a rendered form.

    The test client's cookie jar has already remembered any cookie the page
    set, so echoing the value back is enough to submit the form honestly.
    """
    body = await response.text()

    match = _HIDDEN.search(body)

    assert match, f"the form held no hidden field named {name}:\n{body[:400]}"
    assert match.group(1) == name, f"{match.group(1)!r} is not {name!r}"

    return match.group(2)


_HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)"')


@pytest.fixture
def web_identity():
    return Identity(kind=WEB, id="default")


@pytest.fixture
def notifier():
    return Notifier([TelegramChannel(bot=None), WebChannel()])
