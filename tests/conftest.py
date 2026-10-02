import logging
import os

import pytest

from app.core.identity import WEB, Identity
from app.db.database import Database
from app.db.migrations import MigrationRunner
from app.notifications import Notifier, TelegramChannel, WebChannel
from app.plugins.clipboard import ClipboardPlugin
from app.plugins.manager import PluginManager
from app.plugins.notes import NotesPlugin


@pytest.fixture(scope="session", autouse=True)
def set_test_env():
    os.environ["DB_NAME"] = "automation_platform_test"
    os.environ["DB_HOST"] = "localhost"
    os.environ["DB_PORT"] = "5432"
    os.environ["DB_USER"] = "automation"
    os.environ["DB_PASSWORD"] = ""


@pytest.fixture
def database(set_test_env):
    db = Database()
    _clean_db(db)
    yield db
    _clean_db(db)


@pytest.fixture
def database_with_schema(set_test_env):
    db = Database()
    _clean_db(db)
    MigrationRunner(db).run()
    yield db
    _clean_db(db)


def _clean_db(db: Database):
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DROP TABLE IF EXISTS notes, reminders, clipboard,
                    schema_migrations CASCADE
            """)
        conn.commit()


@pytest.fixture
def manager(database_with_schema):
    mgr = PluginManager(logging.getLogger("test"))
    mgr.register(NotesPlugin(database_with_schema))
    mgr.register(ClipboardPlugin(database_with_schema))
    return mgr


@pytest.fixture
def web_identity():
    return Identity(kind=WEB, id="default")


@pytest.fixture
def notifier():
    return Notifier([TelegramChannel(bot=None), WebChannel()])
