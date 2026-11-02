"""`notify-admins`: a host job's way to reach the administrators."""

import io

import pytest

from app.admin_chats import MESSAGE_LIMIT, send_text
from app.manage import main


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send(token, chats, text):
        calls.append((chats, text))

    monkeypatch.setattr("app.manage.send_text", fake_send)
    monkeypatch.setattr("app.manage.recipients", lambda users, links: [111])
    return calls


def _stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_stdin_goes_to_the_admins(sent, monkeypatch, capsys):
    _stdin(monkeypatch, "SSH за сутки: 12 попыток\n")

    assert main(["notify-admins"]) == 0
    assert sent == [([111], "SSH за сутки: 12 попыток")]
    assert "Sent to 1 chat(s)" in capsys.readouterr().out


def test_empty_stdin_sends_nothing(sent, monkeypatch):
    _stdin(monkeypatch, "  \n")

    assert main(["notify-admins"]) == 1
    assert sent == []


def test_no_linked_admin_is_a_failure(sent, monkeypatch, capsys):
    monkeypatch.setattr("app.manage.recipients", lambda users, links: [])
    _stdin(monkeypatch, "hello")

    assert main(["notify-admins"]) == 1
    assert sent == []
    assert "linked Telegram" in capsys.readouterr().err


def test_a_failed_send_fails_the_run(monkeypatch, capsys):
    async def broken(token, chats, text):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr("app.manage.send_text", broken)
    monkeypatch.setattr("app.manage.recipients", lambda users, links: [111])
    _stdin(monkeypatch, "hello")

    assert main(["notify-admins"]) == 1
    assert "telegram is down" in capsys.readouterr().err


async def test_a_long_text_is_cut_to_one_message(monkeypatch):
    messages = []

    class FakeBot:
        class session:
            @staticmethod
            async def close():
                pass

        async def send_message(self, chat, text):
            messages.append((chat, text))

    monkeypatch.setattr("app.admin_chats.create_bot", lambda token: FakeBot())

    await send_text("token", [1, 2], "x" * (MESSAGE_LIMIT + 10))

    assert [chat for chat, _ in messages] == [1, 2]
    assert all(len(text) == MESSAGE_LIMIT for _, text in messages)
