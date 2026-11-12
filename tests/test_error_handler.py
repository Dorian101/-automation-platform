from types import SimpleNamespace

from aiogram import Dispatcher

from app.bot.error_handler import FAILURE_REPLY, register_error_handler
from app.core.results import CommandError
from app.plugins.manager import MAINTENANCE


class FakeMessage:
    def __init__(self, fails=False):
        self.fails = fails
        self.answers = []

    async def answer(self, text, **kwargs):
        if self.fails:
            raise RuntimeError("telegram is down")
        self.answers.append(text)


def _handler():
    dp = Dispatcher()
    register_error_handler(dp)
    return dp.errors.handlers[0].callback


def _event(message, exception=None):
    return SimpleNamespace(
        exception=exception or RuntimeError("pool timeout"),
        update=SimpleNamespace(message=message),
    )


async def test_the_user_hears_that_something_failed(caplog):
    message = FakeMessage()

    assert await _handler()(_event(message)) is True
    assert message.answers == [FAILURE_REPLY]
    assert "pool timeout" in caplog.text


async def test_an_update_without_a_message_is_only_logged(caplog):
    assert await _handler()(_event(None)) is True
    assert "Unhandled exception" in caplog.text


async def test_a_failed_reply_does_not_escape(caplog):
    assert await _handler()(_event(FakeMessage(fails=True))) is True


async def test_a_refusal_is_answered_with_its_own_text(caplog):
    message = FakeMessage()

    assert await _handler()(_event(message, CommandError(MAINTENANCE))) is True
    assert message.answers == [MAINTENANCE]
    assert "Unhandled exception" not in caplog.text
