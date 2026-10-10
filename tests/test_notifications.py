
import logging

from app.core.identity import WEB, Identity, telegram
from app.notifications import Notifier, TelegramChannel, WebChannel


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class TestTelegramChannel:
    async def test_delivers_to_chat_id(self):
        bot = FakeBot()
        channel = TelegramChannel(bot)

        await channel.deliver(telegram(555), "hi")

        assert bot.sent == [(555, "hi")]

    async def test_handles_negative_group_id(self):
        bot = FakeBot()
        channel = TelegramChannel(bot)

        await channel.deliver(telegram(-1001234), "group message")

        assert bot.sent == [(-1001234, "group message")]


class TestWebChannel:
    async def test_does_not_raise(self):
        channel = WebChannel()

        await channel.deliver(Identity(kind=WEB, id="default"), "hi")

    async def test_the_text_is_withheld_from_the_log(self, caplog):
        """The message is the user's own content and stays out of the journal."""
        channel = WebChannel()

        with caplog.at_level(logging.INFO, logger="app.notifications"):
            await channel.deliver(
                Identity(kind=WEB, id="default"),
                "super-secret-note-text",
            )

        assert "super-secret-note-text" not in caplog.text
        assert "text withheld" in caplog.text


class TestNotifier:
    async def test_routes_to_telegram_channel(self):
        bot = FakeBot()
        notifier = Notifier([TelegramChannel(bot), WebChannel()])

        await notifier.deliver(telegram(42), "telegram message")

        assert bot.sent == [(42, "telegram message")]

    async def test_unknown_transport_is_dropped(self):
        notifier = Notifier([WebChannel()])

        await notifier.deliver(telegram(42), "no channel for this")

    async def test_kinds_reports_registered(self):
        notifier = Notifier([TelegramChannel(bot=None), WebChannel()])

        assert sorted(notifier.kinds()) == ["telegram", "web"]


class RecordingChannel:
    """A channel that remembers what it was asked to deliver."""

    def __init__(self, kind: str):
        self.kind = kind
        self.received: list[tuple[Identity, str]] = []

    async def deliver(self, target, text):
        self.received.append((target, text))


class TestNotifierReportsDelivery:
    """The caller must be able to tell delivered from merely handled."""

    async def test_a_delivered_message_reports_true(self):
        telegram_channel = RecordingChannel("telegram")
        notifier = Notifier([telegram_channel])

        assert await notifier.deliver(telegram(42), "hi") is True

    async def test_a_missing_channel_reports_false(self):
        """Silently dropping is the behaviour being corrected."""
        notifier = Notifier([WebChannel()])

        assert await notifier.deliver(telegram(42), "hi") is False

    async def test_the_web_placeholder_counts_as_delivered(self):
        """It is a channel that accepted the message.

        Reporting False here would make an unlinked deployment give up on
        reminders that were previously only logged, which is a behaviour change
        nobody asked for.
        """
        notifier = Notifier([WebChannel()])

        assert await notifier.deliver(Identity(kind=WEB, id="a"), "hi") is True

    async def test_a_failing_resolver_reports_false(self):
        def broken(target):
            raise RuntimeError("database is down")

        notifier = Notifier([RecordingChannel("web")], resolver=broken)

        assert await notifier.deliver(telegram(1), "hi") is False

    async def test_a_failing_resolver_reaches_no_channel(self):
        sent = RecordingChannel("web")

        def broken(target):
            raise RuntimeError("database is down")

        notifier = Notifier([sent], resolver=broken)
        await notifier.deliver(telegram(1), "hi")

        assert sent.received == []
