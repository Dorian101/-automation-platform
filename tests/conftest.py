# ruff: noqa: E402

import logging
import os
import re
from pathlib import Path

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
from app.plugins.applications import ApplicationsPlugin
from app.plugins.clipboard import ClipboardPlugin
from app.plugins.expenses import ExpensesPlugin
from app.plugins.manager import PluginManager
from app.plugins.notes import NotesPlugin
from app.plugins.reminders import RemindersPlugin
from app.web.auth import AUTH_HEADER
from app.web.server import WebServer

TEST_USERNAME = "alexey"
TEST_PASSWORD = "correct-horse-battery"

#: The scrypt cost every test runs at.
#:
#: The shipped one is 2^17, which is OWASP's figure for non-interactive use
#: and takes about a quarter of a second per hash. At one account per test and
#: several hundred tests, that alone was most of the suite's wall clock — the
#: tests were measuring the password module and not the thing under test.
#:
#: The cost is recorded inside every hash, so a hash written at this value
#: still verifies at the shipped one. What is being thrown away is the work
#: factor, and nothing else: the algorithm, the salt, the stored format, the
#: comparison and the parameters are the production ones.
#:
#: `test_passwords.py` keeps one test that hashes at the real cost, so that
#: the configuration this replaces is still known to work.
TEST_SCRYPT_N = 2**10

@pytest.fixture(scope="session")
def shipped_scrypt_cost() -> tuple[int, int]:
    """`(n, r)` the module is configured with, read before anything patches it.

    A fixture rather than a module constant because `tests/conftest.py` under
    the name `tests.conftest` is a *second* module object: a test importing the
    constant from that name would evaluate the file again, after the patch, and
    read back the cheap figure. Depending on the fixture also fixes the order,
    which is what makes "before" mean anything.
    """
    import app.core.passwords as passwords

    return passwords.SCRYPT_N, passwords.SCRYPT_R


@pytest.fixture(autouse=True, scope="session")
def _cheap_scrypt(shipped_scrypt_cost):
    """Every test hashes cheaply unless it says otherwise."""
    import app.core.passwords as passwords

    passwords.SCRYPT_N = TEST_SCRYPT_N

    yield

    passwords.SCRYPT_N = shipped_scrypt_cost[0]


@pytest.fixture(scope="session")
def _schema():
    """Build the schema once for the whole run.

    Migrations were being applied per test — nine files, each a statement or
    three — and dropped again on both sides. Measured at 91 ms against 5 ms
    for emptying the tables, so it was very nearly all of the per-test setup
    and none of it was testing anything the tests had not already asked for.
    """
    db = Database()
    _clean_db(db)
    MigrationRunner(db).run()

    yield

    _clean_db(db)


@pytest.fixture
def database_with_schema(_schema):
    """A schema, and an empty one.

    TRUNCATE rather than DROP: the tables are the same tables the migrations
    made, and emptying them is what a test needs. RESTART IDENTITY resets the
    sequences, which a drop would have done too.
    """
    db = Database()
    _ensure_schema(db)
    _empty_db(db)
    yield db
    _empty_db(db)


def _ensure_schema(db: Database) -> None:
    """Rebuild the schema if a test has taken it away.

    The tests that run the migrations themselves drop the tables on the way
    in, and the schema is shared for the whole session — so without this, one
    such test leaves every test after it addressing tables that are not there.
    The failure points at whatever ran next, which is the worst possible
    place for it to point.

    One cheap query in the ordinary case. `users` is the first table the first
    migration makes, so its absence means the schema is gone rather than
    partly gone.
    """
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.users') IS NOT NULL")
            present = cur.fetchone()[0]

    if present:
        return

    _clean_db(db)
    MigrationRunner(db).run()


@pytest.fixture
def database(_schema):
    """Nothing at all: not even a schema.

    The tests that take this one go on to run the migrations themselves, and a
    migration that finds its table already there is a `DuplicateTable` — so
    this fixture has to hand over a genuinely empty database, which is why it
    is not the cheap one above.
    """
    db = Database()
    _clean_db(db)
    yield db
    _clean_db(db)


def _empty_db(db: Database):
    """Empty every table without dropping it.

    Enumerated rather than listed by name, for the same reason `_clean_db` is:
    a table added by a later migration has to be emptied without anyone
    remembering to add it here.

    **`schema_migrations` is left alone.** It is not test data, it is the
    ledger of which migrations have been applied — and the schema it describes
    now lives for the whole session. Emptying it would leave the tables in
    place and the ledger claiming they are not there, which is the exact
    state a second `MigrationRunner.run()` reads as "apply all nine" and
    fails on the first.
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
                      AND tablename <> 'schema_migrations'
                    LOOP
                        EXECUTE format(
                            'TRUNCATE TABLE %I RESTART IDENTITY CASCADE', tbl
                        );
                    END LOOP;
                END
                $$
            """)
        conn.commit()


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
def web_server_factory():
    """Build a `WebServer` pointed at a test database.

    Six test modules each wanted one, and they differed only in which plugins
    were registered. The arguments were seven lines of copying per module,
    which is how a host and a port quietly end up different in two of them.
    """

    def _make(manager, database, **overrides):
        settings = {"host": "127.0.0.1", "port": 0}
        settings.update(overrides)

        return WebServer(
            manager=manager, database=database, **settings
        )

    return _make


@pytest.fixture
def manager(database_with_schema):
    mgr = PluginManager(logging.getLogger("test"))
    mgr.register(NotesPlugin(database_with_schema))
    mgr.register(ClipboardPlugin(database_with_schema))
    return mgr


@pytest.fixture
def every_plugin(manager, database_with_schema):
    """Every plugin the application ships.

    A fixture that registers part of them is fine for a test about routing and
    wrong for one that checks what the public pages *name*: a page that lists
    /remind is not wrong because the fixture forgot reminders.
    """
    manager.register(RemindersPlugin(database_with_schema))

    return manager


@pytest.fixture
def pages_manager(manager, database_with_schema):
    """A manager holding every plugin that owns a web page.

    Expenses and applications are registered together so a test about page
    routing sees the real shape of the route table rather than a single
    plugin's view of it.
    """
    manager.register(ExpensesPlugin(database_with_schema))
    manager.register(ApplicationsPlugin(database_with_schema))
    return manager


# Kept under the old name so the spending tests read as they were written.
expenses_manager = pages_manager


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
    from_address: str | None = None,
):
    """Submit the login form the way a browser behind the proxy would.

    The transport header is added when a token is configured, exactly as the
    reverse proxy does in production. Tests that disable the token get no
    header, which is the point of disabling it.

    `from_address` sets the address the proxy would have written, for the tests
    that care about who a request is attributed to.
    """
    headers = (
        {AUTH_HEADER: Config.WEB_ACCESS_TOKEN} if Config.WEB_ACCESS_TOKEN else {}
    )

    if from_address is not None:
        headers["X-Forwarded-For"] = from_address

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


# A template placeholder: double underscores around an upper-case name. The
# case is what keeps `__pycache__` and a docstring's `__init__` out of it.
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")


def template_placeholders(name: str) -> set[str]:
    """Every substitution a template asks for.

    Read from the template rather than listed here, so a placeholder added
    later is covered without remembering to add it to a test.

    Naming the placeholders is the point. Searching the rendered page for
    `__` instead also matches the random values rendered into it: the CSRF
    token is urlsafe base64 and the link nonce is urlsafe random, and both
    alphabets contain `_`. Roughly one page render in a hundred carries a
    `__` inside one of them, which made a page-with-no-placeholder test fail
    about once every hundred runs for a reason that had nothing to do with
    substitutions.
    """
    source = (
        Path(__file__).resolve().parent.parent / "app" / "web" / "templates" / name
    ).read_text(encoding="utf-8")

    return set(_PLACEHOLDER.findall(source))


def assert_no_placeholders(name: str, body: str) -> None:
    """Fail on any substitution the renderer left behind."""
    for placeholder in sorted(template_placeholders(name)):
        assert placeholder not in body, (
            f"{placeholder} survived into the page rendered from {name}"
        )
