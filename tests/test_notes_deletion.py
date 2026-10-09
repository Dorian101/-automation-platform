"""Deleting notes, and the numbering that addresses them.

A note is addressed by its position in the list the person is looking at, not
by its row id. That is a deliberate choice with two consequences worth testing:
the id never leaks, and one person's number cannot reach another's note.
"""

import pytest

from app.core.identity import WEB, Identity, telegram
from app.core.person import PersonResolver
from app.core.results import CommandError
from app.db.links_repo import TelegramLinksRepository
from app.db.notes_repo import NotesRepository
from app.db.users_repo import UsersRepository
from app.plugins.notes import NotesPlugin
from tests.conftest import TEST_USERNAME

CHAT_ID = 4242


@pytest.fixture
def web() -> Identity:
    """The web identity of the default account."""
    return Identity(kind=WEB, id=TEST_USERNAME)


WEB_IDENTITY = Identity(kind=WEB, id=TEST_USERNAME)


@pytest.fixture
def plugin(database_with_schema):
    return NotesPlugin(database_with_schema)


@pytest.fixture
def users(database_with_schema):
    return UsersRepository(database_with_schema)


@pytest.fixture
def links(database_with_schema):
    return TelegramLinksRepository(database_with_schema)


@pytest.fixture
def repo(database_with_schema):
    return NotesRepository(database_with_schema)


class TestListing:
    async def test_notes_are_numbered_from_one(self, plugin, web):
        await plugin.execute("/add", "first", web)
        await plugin.execute("/add", "second", web)

        result = await plugin.execute("/notes", "", web)

        assert result.text == "1. second\n2. first"

    async def test_an_empty_list_says_so(self, plugin, web):
        result = await plugin.execute("/notes", "", web)

        assert result.text == "No notes"

    async def test_the_row_id_is_not_shown(self, plugin, repo, create_user, web):
        """The id is a global sequence, so printing it leaks other people's
        activity and gives a number to guess.

        Shifted off one so the id is a value the numbered list could plausibly
        contain, which is the case that would go unnoticed otherwise.
        """
        # A note for somebody else takes id 1, so this account's note is not.
        users_repo = UsersRepository(plugin.repo.database)
        users_repo.create("bob", "correct-horse-battery")
        repo.add(Identity(kind=WEB, id="bob"), "bobs note")

        await plugin.execute("/add", "a note", web)

        row_id = repo.entries(web)[0][0]

        result = await plugin.execute("/notes", "", web)

        assert str(row_id) != "1"
        assert str(row_id) not in result.text
        assert result.text == "1. a note"


class TestDeletingOne:
    async def test_deletes_the_numbered_note(self, plugin, repo, web):
        await plugin.execute("/add", "first", web)
        await plugin.execute("/add", "second", web)

        result = await plugin.execute("/del", "1", web)

        assert result.text == "Deleted note 1"
        assert [text for _, text in repo.entries(web)] == ["first"]

    async def test_the_newest_is_number_one(self, plugin, repo, web):
        await plugin.execute("/add", "older", web)
        await plugin.execute("/add", "newer", web)

        await plugin.execute("/del", "1", web)

        assert [text for _, text in repo.entries(web)] == ["older"]

    async def test_deleting_from_the_end(self, plugin, repo, web):
        await plugin.execute("/add", "first", web)
        await plugin.execute("/add", "second", web)

        await plugin.execute("/del", "2", web)

        assert [text for _, text in repo.entries(web)] == ["second"]

    async def test_the_rest_are_renumbered_afterwards(self, plugin, repo, web):
        """Positions come from the live list, so they shift on their own."""
        await plugin.execute("/add", "first", web)
        await plugin.execute("/add", "second", web)
        await plugin.execute("/add", "third", web)

        await plugin.execute("/del", "2", web)

        listed = await plugin.execute("/notes", "", web)

        assert listed.text == "1. third\n2. first"
        assert [text for _, text in repo.entries(web)] == ["third", "first"]

    async def test_deleting_the_only_note_leaves_none(self, plugin, repo, web):
        await plugin.execute("/add", "only", web)

        await plugin.execute("/del", "1", web)

        assert repo.entries(web) == []

    async def test_surrounding_whitespace_is_tolerated(self, plugin, web):
        await plugin.execute("/add", "only", web)

        result = await plugin.execute("/del", "  1  ", web)

        assert result.text == "Deleted note 1"


class TestDeletingWhatCannotBeDeleted:
    async def test_a_number_that_is_not_there(self, plugin, web):
        await plugin.execute("/add", "only", web)

        with pytest.raises(CommandError, match="No note 5"):
            await plugin.execute("/del", "5", web)

    async def test_a_non_numeric_argument(self, plugin, web):
        with pytest.raises(CommandError, match="Usage"):
            await plugin.execute("/del", "first", web)

    async def test_no_argument(self, plugin, web):
        with pytest.raises(CommandError, match="Usage"):
            await plugin.execute("/del", "", web)

    async def test_zero(self, plugin, web):
        """Position zero is not in a list numbered from one."""
        await plugin.execute("/add", "only", web)

        with pytest.raises(CommandError, match="Usage"):
            await plugin.execute("/del", "0", web)

    async def test_a_negative_number(self, plugin, web):
        await plugin.execute("/add", "only", web)

        with pytest.raises(CommandError, match="Usage"):
            await plugin.execute("/del", "-1", web)

    async def test_deleting_from_an_empty_list(self, plugin, web):
        with pytest.raises(CommandError, match="No note 1"):
            await plugin.execute("/del", "1", web)

    async def test_a_failed_delete_removes_nothing(self, plugin, repo, web):
        await plugin.execute("/add", "only", web)

        with pytest.raises(CommandError):
            await plugin.execute("/del", "9", web)

        assert len(repo.entries(web)) == 1


class TestOneAccountCannotReachAnother:
    async def test_a_guessed_id_reaches_nothing(
        self,
        plugin,
        repo,
        users,
        links,
        create_user,
    ):
        """Ids are a global sequence, so this is the attack the scoping stops.

        Deleting by id alone would mean the lowest id in the table, whoever
        wrote it.
        """
        bob = users.create("bob", "correct-horse-battery")
        repo.add(Identity(kind=WEB, id="bob"), "bob secret")

        with pytest.raises(CommandError, match="No note"):
            await plugin.execute("/del", "1", Identity(kind=WEB, id="alexey"))

        assert [text for _, text in repo.entries(Identity(kind=WEB, id="bob"))] == [
            "bob secret",
        ]
        assert bob.id > 0

    async def test_clearing_leaves_another_account_alone(
        self,
        plugin,
        repo,
        users,
        create_user,
        web,
    ):
        users.create("bob", "correct-horse-battery")
        repo.add(Identity(kind=WEB, id="bob"), "bob secret")
        repo.add(web, "mine")

        await plugin.execute("/clear", "", web)

        assert [text for _, text in repo.entries(Identity(kind=WEB, id="bob"))] == [
            "bob secret",
        ]

    async def test_two_unlinked_transports_stay_apart(self, plugin, repo, web):
        repo.add(web, "web note")
        repo.add(telegram(777), "bot note")

        await plugin.execute("/clear", "", web)

        assert [text for _, text in repo.entries(telegram(777))] == ["bot note"]


class TestClearing:
    async def test_removes_everything(self, plugin, repo, web):
        await plugin.execute("/add", "one", web)
        await plugin.execute("/add", "two", web)

        result = await plugin.execute("/clear", "", web)

        assert result.text == "Deleted 2 notes"
        assert repo.entries(web) == []

    async def test_a_single_note_reads_naturally(self, plugin, web):
        await plugin.execute("/add", "only", web)

        result = await plugin.execute("/clear", "", web)

        assert result.text == "Deleted 1 note"

    async def test_nothing_to_clear_says_so(self, plugin, web):
        result = await plugin.execute("/clear", "", web)

        assert result.text == "No notes to delete"

    async def test_clearing_is_not_additive(self, plugin, repo, web):
        await plugin.execute("/add", "one", web)

        await plugin.execute("/clear", "", web)
        await plugin.execute("/clear", "", web)

        assert repo.entries(web) == []


class TestDeletionAcrossALinkedPair:
    @pytest.fixture
    def linked_plugin(
        self,
        create_user,
        database_with_schema,
        users,
        links,
    ):
        links.link(users.get_by_username(TEST_USERNAME).id, CHAT_ID)

        return NotesPlugin(database_with_schema, persons=PersonResolver(users, links))

    async def test_a_bot_note_can_be_deleted_from_the_web(
        self,
        linked_plugin,
        repo,
        web,
    ):
        """The case that matters: it is visible there, so it must be
        removable there. Widening reads without widening deletes would leave a
        note on screen that cannot be got rid of."""
        repo.add(telegram(CHAT_ID), "from the bot")

        result = await linked_plugin.execute("/del", "1", web)

        assert result.text == "Deleted note 1"
        assert repo.entries(telegram(CHAT_ID)) == []

    async def test_a_web_note_can_be_deleted_from_the_bot(
        self,
        linked_plugin,
        repo,
        web,
    ):
        repo.add(web, "from the web")

        await linked_plugin.execute("/del", "1", telegram(CHAT_ID))

        assert repo.entries(web) == []

    async def test_clearing_from_the_web_clears_the_pair(
        self,
        linked_plugin,
        repo,
        web,
    ):
        repo.add(web, "from the web")
        repo.add(telegram(CHAT_ID), "from the bot")

        result = await linked_plugin.execute("/clear", "", web)

        assert result.text == "Deleted 2 notes"
        assert repo.entries([web, telegram(CHAT_ID)]) == []

    async def test_unlinking_restores_the_separation(
        self,
        create_user,
        database_with_schema,
        users,
        links,
        web,
    ):
        """No move-back is needed, because deletion never crossed anything.

        With the link gone there is one identity per read, so the bot sees
        nothing it did not write.
        """
        links.unlink(users.get_by_username(TEST_USERNAME).id)

        plugin = NotesPlugin(database_with_schema)
        plugin.repo.add(telegram(CHAT_ID), "from the bot")

        result = await plugin.execute("/notes", "", web)

        assert result.text == "No notes"


class TestNumberingIsPerRead:
    """Positions belong to the list in front of you, not to the note.

    The same text is position 1 in a list of one and position 2 in a list of
    three. That is what makes a position the right thing to address: it is the
    only address the person holding the list can actually see.
    """

    async def test_a_position_is_relative_to_the_list(
        self,
        plugin,
        repo,
        web,
    ):
        repo.add(web, "oldest")
        repo.add(web, "middle")
        repo.add(web, "newest")

        listed = await plugin.execute("/notes", "", web)

        assert listed.text == "1. newest\n2. middle\n3. oldest"

    async def test_two_people_can_see_the_same_position_holding_different_notes(
        self,
        plugin,
        repo,
        users,
        create_user,
        web,
    ):
        """Position 1 means "your newest", which is per person by definition."""
        users.create("bob", "correct-horse-battery")
        bob = Identity(kind=WEB, id="bob")

        repo.add(web, "alexeys older")
        repo.add(web, "alexeys newest")
        repo.add(bob, "bobs only note")

        mine = await plugin.execute("/notes", "", web)
        theirs = await plugin.execute("/notes", "", bob)

        assert mine.text == "1. alexeys newest\n2. alexeys older"
        assert theirs.text == "1. bobs only note"

    async def test_deleting_position_one_removes_your_newest(
        self,
        plugin,
        repo,
        web,
    ):
        repo.add(web, "older")
        repo.add(web, "newer")

        await plugin.execute("/del", "1", web)

        assert [text for _, text in repo.entries(web)] == ["older"]
