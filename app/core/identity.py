from dataclasses import dataclass

TELEGRAM = "telegram"
WEB = "web"


class InvalidIdentity(ValueError):
    """Raised when an identity string cannot be parsed."""


@dataclass(frozen=True)
class Identity:
    """A user identity, namespaced by the transport it arrived through.

    Stored as ``kind:id`` so that the same numeric id on two different
    transports never resolves to the same user.
    """

    kind: str
    id: str

    def __post_init__(self) -> None:
        if not self.kind:
            raise InvalidIdentity("identity kind must not be empty")

        if not self.id:
            raise InvalidIdentity("identity id must not be empty")

    @classmethod
    def parse(cls, value: str) -> "Identity":
        kind, separator, identity_id = value.partition(":")

        if not separator or not kind or not identity_id:
            raise InvalidIdentity(f"malformed identity: {value!r}")

        return cls(kind=kind, id=identity_id)

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"


def telegram(chat_id: int) -> Identity:
    return Identity(kind=TELEGRAM, id=str(chat_id))
