"""Reading one person's data across both transports.

Writes stay in the identity that made them. Reads span the pair. That
asymmetry is the whole design: it is what makes unlinking instant, and it is
what makes the stored history still say who wrote what and from where.
"""

import pytest

from app.core.identity import WEB, Identity, telegram
from app.core.person import PersonResolver
from app.db.links_repo import TelegramLinksRepository
from app.db.notes_repo import NotesRepository
from app.db.users_repo import UsersRepository
from tests.conftest import TEST_USERNAME

CHAT_ID = 4242


def _texts(entries):
    """The note text alone, so assertions do not repeat the row id."""
    return [text for _, text in entries]


@pytest.fixture
def users(database_with_schema):
    return UsersRepository(database_with_schema)


@pytest.fixture
def links(database_with_schema):
    return TelegramLinksRepository(database_with_schema)


@pytest.fixture
def notes(database_with_schema):
    return NotesRepository(database_with_schema)


@pytest.fixture
def persons(users, links):
    return PersonResolver(users, links)


@pytest.fixture
def account(create_user, users):
    return users.get_by_username(TEST_USERNAME)


def web() -> Identity:
    return Identity(kind=WEB, id=TEST_USERNAME)


class TestUnpairedIdentities:
    def test_an_unlinked_web_identity_reads_as_itself(self, persons):
        assert persons.identities(web()) == (web(),)

    def test_a_telegram_identity_reads_as_itself(self, persons):
        """Nobody pairs a bot identity from the bot side."""
        assert persons.identities(telegram(42)) == (telegram(42),)

    def test_a_web_identity_with_no_account_reads_as_itself(self, persons):
        unknown = Identity(kind=WEB, id="nobody")

        assert persons.identities(unknown) == (unknown,)

    def test_a_malformed_telegram_identity_is_not_an_error(self, persons):
        """It cannot match a stored chat id, so it means "no pair"."""
        broken = Identity(kind="telegram", id="not-a-number")

        assert persons.identities(broken) == (broken,)

    def test_an_unknown_transport_reads_as_itself(self, persons):
        other = Identity(kind="carrier-pigeon", id="1")

        assert persons.identities(other) == (other,)


class TestPairedIdentities:
    def test_a_web_identity_reads_across_the_pair(
        self,
        persons,
        links,
        account,
    ):
        links.link(account.id, CHAT_ID)

        assert set(persons.identities(web())) == {web(), telegram(CHAT_ID)}

    def test_the_bot_reads_across_the_pair_too(
        self,
        persons,
        links,
        account,
    ):
        """Otherwise pairing works one way, which is not the same person."""
        links.link(account.id, CHAT_ID)

        assert set(persons.identities(telegram(CHAT_ID))) == {
            telegram(CHAT_ID),
            web(),
        }

    def test_the_account_itself_always_comes_first(
        self,
        persons,
        links,
        account,
    ):
        """A read must never lose the identity that asked."""
        links.link(account.id, CHAT_ID)

        assert persons.identities(web())[0] == web()

    def test_unlinking_takes_effect_immediately(self, persons, links, account):
        links.link(account.id, CHAT_ID)
        links.unlink(account.id)

        assert persons.identities(web()) == (web(),)
        assert persons.identities(telegram(CHAT_ID)) == (telegram(CHAT_ID),)

    def test_relinking_moves_the_pair(self, persons, links, account):
        links.link(account.id, CHAT_ID)
        links.unlink(account.id)
        links.link(account.id, 9999)

        assert set(persons.identities(web())) == {web(), telegram(9999)}

    def test_a_group_chat_pairs_too(self, persons, links, account):
        """Chat ids are negative for groups and the column holds both."""
        group = -1001234567890
        links.link(account.id, group)

        assert telegram(group) in persons.identities(web())


class TestTwoAccountsStayApart:
    def test_pairing_one_does_not_widen_the_other(
        self,
        persons,
        links,
        users,
        account,
    ):
        """The one-to-one constraint, seen from reading."""
        users.create("bob", "correct-horse-battery")
        links.link(account.id, CHAT_ID)

        bob_identity = Identity(kind=WEB, id="bob")

        assert persons.identities(bob_identity) == (bob_identity,)
        assert telegram(9999) not in persons.identities(bob_identity)

    def test_the_other_accounts_chat_is_not_reachable(self, persons, links, account):
        links.link(account.id, CHAT_ID)

        # 9999 belongs to nobody, so it stays unread.
        assert telegram(9999) not in persons.identities(telegram(CHAT_ID))


class TestNotesReadAcrossThePair:
    def test_a_note_written_in_the_bot_is_visible_on_the_web(
        self,
        notes,
        persons,
        links,
        account,
    ):
        notes.add(telegram(CHAT_ID), "written in the bot")
        links.link(account.id, CHAT_ID)

        assert _texts(notes.entries(persons.identities(web()))) == [
            "written in the bot",
        ]

    def test_a_note_written_on_the_web_is_visible_in_the_bot(
        self,
        notes,
        persons,
        links,
        account,
    ):
        notes.add(web(), "written on the web")
        links.link(account.id, CHAT_ID)

        assert _texts(
            notes.entries(persons.identities(telegram(CHAT_ID)))
        ) == ["written on the web"]

    def test_both_transports_are_read_as_one_history(
        self,
        notes,
        persons,
        links,
        account,
    ):
        notes.add(web(), "from the web")
        notes.add(telegram(CHAT_ID), "from the bot")
        links.link(account.id, CHAT_ID)

        read = _texts(notes.entries(persons.identities(web())))

        assert sorted(read) == ["from the bot", "from the web"]

    def test_the_history_interleaves_by_when_it_was_written(
        self,
        notes,
        persons,
        links,
        account,
    ):
        """One list in real order, not two lists joined."""
        notes.add(web(), "first")
        notes.add(telegram(CHAT_ID), "second")
        notes.add(web(), "third")
        links.link(account.id, CHAT_ID)

        assert _texts(notes.entries(persons.identities(web()))) == [
            "third",
            "second",
            "first",
        ]

    def test_an_unlinked_account_reads_only_its_own(
        self,
        notes,
        persons,
    ):
        notes.add(web(), "mine")
        notes.add(telegram(CHAT_ID), "not mine")

        assert _texts(notes.entries(persons.identities(web()))) == ["mine"]

    def test_nothing_is_written_to_the_other_identity(
        self,
        notes,
        persons,
        links,
        account,
    ):
        """Widening a read must not quietly widen a write too."""
        notes.add(web(), "mine")
        links.link(account.id, CHAT_ID)

        assert _texts(notes.entries(telegram(CHAT_ID))) == []

    def test_another_persons_notes_are_never_readable(
        self,
        notes,
        persons,
        links,
        users,
        account,
    ):
        bob = users.create("bob", "correct-horse-battery")
        bob_chat = 9999
        notes.add(Identity(kind=WEB, id="bob"), "bob secret")
        links.link(account.id, CHAT_ID)
        links.link(bob.id, bob_chat)

        read = _texts(notes.entries(persons.identities(web())))

        assert "bob secret" not in read


class TestOneIdentityAloneStillWorks:
    def test_a_single_identity_still_lists(self, notes):
        """Callers that pass one identity are unaffected."""
        notes.add(web(), "first")
        notes.add(web(), "second")

        assert _texts(notes.entries(web())) == ["second", "first"]

    def test_an_empty_tuple_reads_nothing(self, notes):
        notes.add(web(), "mine")

        assert _texts(notes.entries(())) == []

    def test_a_list_of_identities_is_accepted(self, notes):
        notes.add(web(), "mine")

        assert _texts(notes.entries([web(), telegram(CHAT_ID)])) == ["mine"]
