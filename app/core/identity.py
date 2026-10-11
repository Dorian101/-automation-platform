from dataclasses import dataclass

TELEGRAM = "telegram"
WEB = "web"

# The namespace an anonymous visitor on the demonstration page is given.
#
# A guest is not an account and is never given one: there is no row in
# `users`, no password and no session. Everything it writes lives under
# `web:guest:<token>`, which is what makes cleaning up after it a delete by
# prefix rather than a walk over the accounts table.
GUEST = "guest"


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


def guest(token: str) -> Identity:
    """The identity of an anonymous visitor on the demonstration page.

    The token is the random half of a cookie and is the whole of what
    distinguishes one visitor from another. It never becomes an account, so
    there is nothing to sign into and nothing to authenticate — which is why
    the demonstration routes must not resolve a session for it.
    """
    return Identity(kind=WEB, id=f"{GUEST}:{token}")


def is_guest(identity: Identity) -> bool:
    """Whether an identity belongs to the demonstration page.

    Read by the plugins to cap what an anonymous visitor may write. A plugin
    asking is not the same as a plugin knowing where the identity came from:
    the prefix is a property of the identity, and a real account can never
    carry it, because a username is restricted to `[A-Za-z0-9._-]`.
    """
    return identity.kind == WEB and identity.id.startswith(f"{GUEST}:")
