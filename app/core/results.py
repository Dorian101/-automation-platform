from dataclasses import dataclass


class CommandError(Exception):
    """Raised when a command cannot be executed.

    Carries a message that is safe to show directly to the user, on any
    transport.
    """


@dataclass(frozen=True)
class CommandResult:
    """What a plugin returns from :meth:`BasePlugin.execute`.

    ``text`` is transport-agnostic and renders identically on every transport.
    Delivering a message to a user later, on their own transport, is a
    separate concern handled by the notification layer.

    ``data`` is an optional structured payload for the web console. It exists
    because a line of text is enough to report a note and not enough to draw a
    spending breakdown: a chart is built from numbers, not from a sentence. A
    transport that does not render it — Telegram — ignores it, so a plugin
    fills both in and each one takes what it can use.

    The payload is a plain dict on purpose. Its shape belongs to the plugin
    that produced it, and the console interprets the handful of kinds it
    knows how to draw, falling back to ``text`` for anything else. Freeing the
    core from the vocabulary is what keeps a new plugin from having to change
    the platform before it can be shown.
    """

    text: str
    data: dict | None = None


def reply(text: str, data: dict | None = None) -> CommandResult:
    return CommandResult(text=text, data=data)
