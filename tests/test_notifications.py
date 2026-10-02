
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
