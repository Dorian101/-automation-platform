"""Where a reminder ends up, given who asked.

The resolver is the whole reason linking exists: a reminder created on the web
is delivered over Telegram, because the two identities belong to one person.
Nothing is merged and no row is rewritten — what moves is the delivery.
"""

import pytest

from app.core.identity import WEB, Identity, telegram
from app.db.links_repo import TelegramLinksRepository
from app.db.users_repo import UsersRepository
from app.notifications import LinkedChatResolver, Notifier, WebChannel
from tests.conftest import TEST_USERNAME

CHAT_ID = 4242


class RecordingChannel:
    def __init__(self, kind: str):
        self.kind = kind
        self.received: list[tuple[Identity, str]] = []

    async def deliver(self, target, text):
        self.received.append((target, text))


@pytest.fixture
def links(database_with_schema):
    return TelegramLinksRepository(database_with_schema)


@pytest.fixture
def users(database_with_schema):
    return UsersRepository(database_with_schema)


@pytest.fixture
def resolver(users, links):
    return LinkedChatResolver(users, links)


@pytest.fixture
def web_user(create_user, users):
    """The default account, which `create_user` has already made."""
    return users.get_by_username(TEST_USERNAME)


class TestLinkedDelivery:
    def test_a_linked_web_identity_delivers_to_telegram(
        self,
        resolver,
        links,
        web_user,
    ):
        links.link(web_user.id, CHAT_ID)

        resolved = resolver(Identity(kind=WEB, id=TEST_USERNAME))

        assert resolved == telegram(CHAT_ID)

    def test_telegram_identities_are_left_alone(self, resolver, links, web_user):
        """Already on the right transport; a link must not disturb that."""
        links.link(web_user.id, CHAT_ID)

        assert resolver(telegram(999)) == telegram(999)

    def test_an_unlinked_web_identity_resolves_to_itself(self, resolver, web_user):
        """Keeps the old behaviour: WebChannel, logged and dropped.

        Returning the identity rather than raising is what lets an unlinked
        deployment work exactly as it did before linking existed.
        """
        assert resolver(Identity(kind=WEB, id=TEST_USERNAME)) == Identity(
            kind=WEB,
            id=TEST_USERNAME,
        )

    def test_a_web_identity_with_no_account_resolves_to_itself(self, resolver):
        """Nothing to look up, so nothing to redirect."""
        unknown = Identity(kind=WEB, id="nobody")

        assert resolver(unknown) == unknown

    def test_an_unlinked_identity_warns_about_where_notifications_go(
        self,
        resolver,
        web_user,
        caplog,
    ):
        """Otherwise a dropped reminder is invisible until it is missed."""
        with caplog.at_level("WARNING"):
            resolver(Identity(kind=WEB, id=TEST_USERNAME))

        assert "No Telegram link" in caplog.text

    def test_a_linked_identity_does_not_warn(self, resolver, links, web_user, caplog):
        links.link(web_user.id, CHAT_ID)

        with caplog.at_level("WARNING"):
            resolver(Identity(kind=WEB, id=TEST_USERNAME))

        assert caplog.text == ""

    def test_unlinking_stops_the_redirection(self, resolver, links, web_user):
        links.link(web_user.id, CHAT_ID)
        links.unlink(web_user.id)

        assert resolver(Identity(kind=WEB, id=TEST_USERNAME)).kind == WEB

    def test_relinking_to_another_chat_moves_the_delivery(
        self,
        resolver,
        links,
        web_user,
    ):
        """Correcting a stale link takes effect without touching any data."""
        links.link(web_user.id, CHAT_ID)
        links.unlink(web_user.id)
        links.link(web_user.id, 9999)

        assert resolver(Identity(kind=WEB, id=TEST_USERNAME)) == telegram(9999)


class TestEndToEnd:
    async def test_a_web_reminder_lands_in_the_chat(
        self,
        resolver,
        links,
        web_user,
    ):
        links.link(web_user.id, CHAT_ID)

        sent = RecordingChannel("telegram")
        notifier = Notifier([sent], resolver=resolver)

        await notifier.deliver(Identity(kind=WEB, id=TEST_USERNAME), "call mum")

        assert sent.received == [(telegram(CHAT_ID), "call mum")]

    async def test_an_unlinked_web_reminder_still_reaches_the_web_channel(
        self,
        resolver,
    ):
        notifier = Notifier([WebChannel()], resolver=resolver)

        assert await notifier.deliver(
            Identity(kind=WEB, id=TEST_USERNAME),
            "call mum",
        ) is True

    async def test_the_bot_never_sees_the_web_username(self, resolver, links, web_user):
        """Delivery carries a chat id, so nothing downstream can mistake the
        web identity for a Telegram one."""
        links.link(web_user.id, CHAT_ID)

        resolved = resolver(Identity(kind=WEB, id=TEST_USERNAME))

        assert TEST_USERNAME not in str(resolved)

    async def test_two_accounts_stay_separate(self, resolver, links, users, web_user):
        """The reason the link is one-to-one, seen from delivery."""
        bob = users.create("bob", "correct-horse-battery")
        links.link(web_user.id, CHAT_ID)
        links.link(bob.id, 9999)

        sent = RecordingChannel("telegram")
        notifier = Notifier([sent], resolver=resolver)

        await notifier.deliver(Identity(kind=WEB, id=TEST_USERNAME), "alexey's")
        await notifier.deliver(Identity(kind=WEB, id="bob"), "bob's")

        assert sent.received == [
            (telegram(CHAT_ID), "alexey's"),
            (telegram(9999), "bob's"),
        ]
