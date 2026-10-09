from dataclasses import dataclass

from .database import Database


@dataclass(frozen=True)
class TelegramLink:
    """One web account paired with one Telegram chat.

    Neither side identifies the person: web data stays under ``web:<name>`` and
    Telegram data stays under ``telegram:<id>``. The link only records that the
    two identities belong together, which is what lets them be read as one
    person's data later without moving a single row.
    """

    user_id: int
    telegram_id: int
    linked_at: str


class TelegramLinksRepository:
    """Storage for the account-to-chat pairing."""

    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def link(self, user_id: int, telegram_id: int) -> TelegramLink:
        """Pair a web account with a Telegram chat.

        Raises ``UniqueViolation`` if either side is already paired, since both
        columns are unique. The caller decides what to tell the user about it:
        which of the two conflicts happened is not something this layer can
        report on its own.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO telegram_links (user_id, telegram_id)
                    VALUES (%s, %s)
                    RETURNING user_id, telegram_id, linked_at
                    """,
                    (user_id, telegram_id),
                )
                row = cur.fetchone()
            conn.commit()

        return TelegramLink(user_id=row[0], telegram_id=row[1], linked_at=str(row[2]))

    def unlink(self, user_id: int) -> bool:
        """Remove the pairing, returning whether there was one."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM telegram_links WHERE user_id = %s",
                    (user_id,),
                )
                removed = cur.rowcount
            conn.commit()

        return removed > 0

    def get_by_user_id(self, user_id: int) -> TelegramLink | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, telegram_id, linked_at
                    FROM telegram_links
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                row = cur.fetchone()

        return _to_link(row)

    def get_by_telegram_id(self, telegram_id: int) -> TelegramLink | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, telegram_id, linked_at
                    FROM telegram_links
                    WHERE telegram_id = %s
                    """,
                    (telegram_id,),
                )
                row = cur.fetchone()

        return _to_link(row)

    def telegram_id_for(self, user_id: int) -> int | None:
        """Return the chat a user's notifications should go to.

        Kept as a separate short method because this is the only question the
        delivery layer asks, and asking it should not require loading a row
        the caller will never read.
        """
        link = self.get_by_user_id(user_id)

        return link.telegram_id if link else None

    def list_all(self) -> list[TelegramLink]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, telegram_id, linked_at
                    FROM telegram_links
                    ORDER BY linked_at DESC
                    """,
                )
                rows = cur.fetchall()

        return [_to_link(row) for row in rows]


def _to_link(row) -> TelegramLink | None:
    if row is None:
        return None

    return TelegramLink(user_id=row[0], telegram_id=row[1], linked_at=str(row[2]))
