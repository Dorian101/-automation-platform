import pytest
from psycopg.errors import UniqueViolation

from app.core.identity import WEB, Identity, telegram
from app.db.links_repo import TelegramLinksRepository
from app.db.notes_repo import NotesRepository
from tests.conftest import TEST_USERNAME

CHAT_ID = 4242


@pytest.fixture
def links(database_with_schema):
    return TelegramLinksRepository(database_with_schema)


@pytest.fixture
def user(create_user, users):
    """The default account, which `create_user` has already made."""
    return users.get_by_username(TEST_USERNAME)


@pytest.fixture
def notes(database_with_schema):
    return NotesRepository(database_with_schema)


class TestLinking:
    def test_link_returns_the_pairing(self, links, user):
        link = links.link(user.id, CHAT_ID)

        assert link.user_id == user.id
        assert link.telegram_id == CHAT_ID

    def test_link_is_readable_from_both_sides(self, links, user):
        links.link(user.id, CHAT_ID)

        assert links.get_by_user_id(user.id).telegram_id == CHAT_ID
        assert links.get_by_telegram_id(CHAT_ID).user_id == user.id

    def test_telegram_id_for_answers_the_delivery_question(self, links, user):
        links.link(user.id, CHAT_ID)

        assert links.telegram_id_for(user.id) == CHAT_ID

    def test_telegram_id_for_an_unlinked_user_is_none(self, links, user):
        assert links.telegram_id_for(user.id) is None

    def test_a_missing_link_reads_as_nothing(self, links):
        assert links.get_by_user_id(1) is None
        assert links.get_by_telegram_id(CHAT_ID) is None


class TestOneToOne:
    """One chat, one account.

    Without this a shared login would merge two people's data, which is the
    failure the whole table exists to prevent.
    """

    def test_a_chat_cannot_be_linked_twice(self, links, create_user):
        links.link(create_user("alice").id, CHAT_ID)

        with pytest.raises(UniqueViolation):
            links.link(create_user("bob").id, CHAT_ID)

    def test_an_account_cannot_have_two_chats(self, links, user):
        links.link(user.id, CHAT_ID)

        with pytest.raises(UniqueViolation):
            links.link(user.id, 9999)

    def test_a_failed_second_link_leaves_the_first_intact(
        self,
        links,
        create_user,
    ):
        alice = create_user("alice")
        links.link(alice.id, CHAT_ID)

        with pytest.raises(UniqueViolation):
            links.link(create_user("bob").id, CHAT_ID)

        assert links.get_by_telegram_id(CHAT_ID).user_id == alice.id
        assert links.get_by_user_id(alice.id).telegram_id == CHAT_ID

    def test_chats_can_be_negative_for_groups(self, links, user):
        link = links.link(user.id, -1001234567890)

        assert links.get_by_user_id(link.user_id).telegram_id == -1001234567890


class TestUnlinking:
    def test_unlink_removes_the_pairing(self, links, user):
        links.link(user.id, CHAT_ID)

        assert links.unlink(user.id) is True
        assert links.get_by_user_id(user.id) is None

    def test_unlink_reports_there_was_nothing_to_remove(self, links, user):
        assert links.unlink(user.id) is False

    def test_a_freed_chat_can_be_linked_again(self, links, create_user):
        alice = create_user("alice")
        links.link(alice.id, CHAT_ID)
        links.unlink(alice.id)

        assert links.link(create_user("bob").id, CHAT_ID).telegram_id == CHAT_ID

    def test_deleting_the_account_removes_the_link(
        self,
        links,
        user,
        database_with_schema,
    ):
        links.link(user.id, CHAT_ID)

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE id = %s", (user.id,))
            conn.commit()

        assert links.get_by_telegram_id(CHAT_ID) is None


class TestLinkingMovesNoData:
    """The link records a relationship; it never relocates anything.

    Notes written before the link stay under the identity that wrote them. This
    is the property that makes linking safe to do at any time: if it moved rows,
    unlinking would have no way to put them back.
    """

    def test_web_notes_stay_under_web_after_linking(self, notes, links, user):
        notes.add(Identity(kind=WEB, id=TEST_USERNAME), "written on the web")
        links.link(user.id, CHAT_ID)

        assert notes.list(Identity(kind=WEB, id=TEST_USERNAME)) == [
            "written on the web",
        ]

    def test_telegram_notes_are_reachable_after_linking(self, notes, links, user):
        notes.add(telegram(CHAT_ID), "written in the bot")
        links.link(user.id, CHAT_ID)

        # Still stored as telegram:<id>, not rewritten to web:<name>. Reading
        # them together is the delivery layer's job, not the link's.
        assert notes.list(telegram(CHAT_ID)) == ["written in the bot"]
        assert notes.list(Identity(kind=WEB, id=TEST_USERNAME)) == []


class TestListing:
    def test_empty_without_links(self, links):
        assert links.list_all() == []

    def test_newest_link_first(self, links, create_user):
        alice = create_user("alice")
        bob = create_user("bob")
        links.link(alice.id, CHAT_ID)
        links.link(bob.id, 9999)

        assert [pair.user_id for pair in links.list_all()] == [bob.id, alice.id]
