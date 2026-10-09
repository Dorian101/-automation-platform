"""The link commands on the command line.

These are the way out when the browser cannot reach Telegram, so they are the
only way to undo a link made by mistake. That makes their failure messages part
of the recovery path, not just console output: a wrong link has to be
diagnosable from the host with nothing else to hand.
"""

import pytest

from app.db.links_repo import TelegramLinksRepository
from app.db.users_repo import UsersRepository
from app.manage import main
from tests.conftest import TEST_PASSWORD, TEST_USERNAME

CHAT_ID = 4242


@pytest.fixture
def cli(database_with_schema):
    """Run the commands against the test schema, as the real thing does."""
    users = UsersRepository(database_with_schema)
    users.create(TEST_USERNAME, TEST_PASSWORD)

    def _run(*argv: str) -> int:
        return main(list(argv))

    return _run


@pytest.fixture
def stored(database_with_schema):
    return TelegramLinksRepository(database_with_schema), UsersRepository(
        database_with_schema
    )


def _account_id(stored, username=TEST_USERNAME) -> int:
    links, users = stored
    user = users.get_by_username(username)

    assert user is not None, "the account the command should have found"

    return user.id


class TestLinkCommand:
    def test_links_an_account(self, cli, stored):
        assert cli("link-telegram", TEST_USERNAME, str(CHAT_ID)) == 0

        links, _ = stored
        assert links.get_by_telegram_id(CHAT_ID) is not None

    def test_an_unknown_account_fails_without_linking(self, cli, stored):
        assert cli("link-telegram", "nobody", str(CHAT_ID)) == 1

        links, _ = stored
        assert links.list_all() == []

    def test_a_chat_id_that_is_not_a_number_is_refused(self, cli, stored, capsys):
        assert cli("link-telegram", TEST_USERNAME, "notanumber") == 1

        assert "must be a number" in capsys.readouterr().err

        links, _ = stored
        assert links.list_all() == []

    def test_a_negative_chat_id_is_accepted(self, cli, stored):
        assert cli("link-telegram", TEST_USERNAME, "-1001234567890") == 0

        links, _ = stored
        assert links.get_by_telegram_id(-1001234567890) is not None

    def test_the_username_is_matched_case_insensitively(self, cli, stored):
        assert cli("link-telegram", TEST_USERNAME.upper(), str(CHAT_ID)) == 0

        links, _ = stored
        assert links.get_by_telegram_id(CHAT_ID).user_id == _account_id(stored)


class TestOneAccountOneChat:
    def test_a_chat_already_linked_is_reported_as_such(
        self,
        cli,
        stored,
        capsys,
        database_with_schema,
    ):
        cli("link-telegram", TEST_USERNAME, str(CHAT_ID))
        UsersRepository(database_with_schema).create("bob", TEST_PASSWORD)

        assert cli("link-telegram", "bob", str(CHAT_ID)) == 1

        assert "already linked to another account" in capsys.readouterr().err

    def test_the_conflict_does_not_move_the_chat(self, cli, stored):
        cli("link-telegram", TEST_USERNAME, str(CHAT_ID))
        alice = _account_id(stored)

        UsersRepository(stored[0].database).create("bob", TEST_PASSWORD)
        cli("link-telegram", "bob", str(CHAT_ID))

        links, _ = stored
        assert links.get_by_telegram_id(CHAT_ID).user_id == alice


class TestReplacingALink:
    def test_a_new_chat_replaces_the_old_one(self, cli, stored, capsys):
        cli("link-telegram", TEST_USERNAME, str(CHAT_ID))

        assert cli("link-telegram", TEST_USERNAME, "9999") == 0

        links, _ = stored
        assert links.get_by_telegram_id(9999) is not None
        assert links.get_by_telegram_id(CHAT_ID) is None

        # Said out loud rather than done quietly: a link replaced by accident
        # is otherwise invisible until the notifications stop arriving.
        assert "Replaced link" in capsys.readouterr().out

    def test_relinking_the_same_chat_is_a_no_op(self, cli, stored, capsys):
        cli("link-telegram", TEST_USERNAME, str(CHAT_ID))

        assert cli("link-telegram", TEST_USERNAME, str(CHAT_ID)) == 0

        assert "already linked" in capsys.readouterr().out
        links, _ = stored
        assert links.get_by_telegram_id(CHAT_ID) is not None

    def test_replacing_keeps_one_link_per_account(self, cli, stored):
        cli("link-telegram", TEST_USERNAME, str(CHAT_ID))
        cli("link-telegram", TEST_USERNAME, "9999")

        links, _ = stored
        assert len(links.list_all()) == 1


class TestUnlinkCommand:
    def test_unlink_removes_the_link(self, cli, stored):
        cli("link-telegram", TEST_USERNAME, str(CHAT_ID))

        assert cli("unlink-telegram", TEST_USERNAME) == 0

        links, _ = stored
        assert links.list_all() == []

    def test_unlinking_nothing_succeeds_quietly(self, cli, capsys):
        assert cli("unlink-telegram", TEST_USERNAME) == 0

        assert "not linked" in capsys.readouterr().out

    def test_unlinking_an_unknown_account_fails(self, cli):
        assert cli("unlink-telegram", "nobody") == 1

    def test_a_freed_chat_can_be_linked_by_someone_else(self, cli, stored, capsys):
        UsersRepository(stored[0].database).create("bob", TEST_PASSWORD)
        cli("link-telegram", TEST_USERNAME, str(CHAT_ID))
        cli("unlink-telegram", TEST_USERNAME)

        assert cli("link-telegram", "bob", str(CHAT_ID)) == 0

        links, _ = stored
        assert links.get_by_telegram_id(CHAT_ID) is not None


class TestListLinksCommand:
    def test_says_how_to_make_the_first_one(self, cli, capsys):
        assert cli("list-links") == 0

        assert "link-telegram" in capsys.readouterr().out

    def test_shows_the_account_and_the_chat(self, cli, stored, capsys):
        cli("link-telegram", TEST_USERNAME, str(CHAT_ID))

        assert cli("list-links") == 0

        out = capsys.readouterr().out
        assert TEST_USERNAME in out
        assert str(CHAT_ID) in out
