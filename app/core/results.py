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
    """

    text: str


def reply(text: str) -> CommandResult:
    return CommandResult(text=text)
