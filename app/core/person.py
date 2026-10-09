"""Which identities hold one person's data.

A web account and a Telegram chat are two namespaces on purpose, so the same
numeric id on each transport is not the same person. Pairing them says they
are, and this module turns that into the set of identities a read should look
across.

Writes are deliberately not widened. A note added on the web is stored under
the web identity and stays there, so unlinking takes effect at once and the
history keeps saying who wrote what and from where. Only reading spans the
pair, and it spans exactly the pairs that exist — nothing here invents a
shared namespace.
"""

from app.core.identity import TELEGRAM, WEB, Identity


class PersonResolver:
    """Expands one identity into every identity that belongs to the same person."""

    def __init__(self, users, links):
        self._users = users
        self._links = links

    def identities(self, identity: Identity) -> tuple[Identity, ...]:
        """Return the identity itself, plus any it is paired with.

        An identity with no pair — every Telegram user, and any web account
        that never linked — comes back alone. That is the same result as
        asking for one row, so an unpaired deployment behaves exactly as it
        did before pairing existed.
        """
        paired = self._paired_with(identity)

        return (identity,) if paired is None else (identity, paired)

    def _paired_with(self, identity: Identity) -> Identity | None:
        if identity.kind == WEB:
            return self._paired_with_web(identity)
        if identity.kind == TELEGRAM:
            return self._paired_with_telegram(identity)

        return None

    def _paired_with_web(self, identity: Identity) -> Identity | None:
        user = self._users.get_by_username(identity.id)

        if user is None:
            return None

        link = self._links.get_by_user_id(user.id)

        return None if link is None else _telegram(link.telegram_id)

    def _paired_with_telegram(self, identity: Identity) -> Identity | None:
        """The reverse direction, for a command arriving through the bot.

        Without it, pairing would be one-directional: the web account would see
        the bot's notes and the bot would not see the account's. A pair that
        only reads one way is not the same person.
        """
        # Not parsed, compared as a string. A chat id that is not a number
        # cannot match a stored one, and rejecting it would turn a malformed
        # identity into an error rather than the "no pair" it really is.
        link = self._links.get_by_telegram_id_or_none(identity.id)

        if link is None:
            return None

        user = self._users.get_by_id(link.user_id)

        return None if user is None else Identity(kind=WEB, id=user.username)


def _telegram(chat_id) -> Identity:
    return Identity(kind=TELEGRAM, id=str(chat_id))
