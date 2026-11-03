import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

from .database import Database

# Long enough that a leaked dump cannot be replayed, short enough to fit the
# column comfortably.
TOKEN_BYTES = 32

#: How often a session's `last_seen` is written. Short enough for the admin
#: area's "in use now" window, long enough that a page of requests is one
#: write, not one each.
TOUCH_SECONDS = 300

#: A session used within this window counts as in use on /admin/users.
ACTIVE_MINUTES = 30


@dataclass(frozen=True)
class ActiveSession:
    id: int
    username: str
    created_at: datetime
    expires_at: datetime
    agent: str | None
    token_hash: str
    last_seen: datetime


class SessionsRepository:
    """Web sessions, keyed by the SHA-256 of the cookie value.

    The raw token exists only in the cookie. Storing the digest means a dump of
    this table yields nothing usable, and removing the row is sufficient to log
    a session out.
    """

    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def create(self, user_id: int, ttl_seconds: int, agent: str | None = None) -> str:
        token = secrets.token_urlsafe(TOKEN_BYTES)

        # Expiry is computed against the database clock on both write and read
        # so the two can never disagree, whatever the server timezone is.
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sessions (token_hash, user_id, expires_at, agent)
                    VALUES (
                        %s,
                        %s,
                        CURRENT_TIMESTAMP + make_interval(secs => %s),
                        %s
                    )
                    """,
                    (_digest(token), user_id, ttl_seconds, agent),
                )
            conn.commit()

        self.delete_expired()

        return token

    def get_user_id(self, token: str) -> int | None:
        if not token:
            return None

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id
                    FROM sessions
                    WHERE token_hash = %s
                      AND expires_at > CURRENT_TIMESTAMP
                    """,
                    (_digest(token),),
                )
                row = cur.fetchone()

                if row is not None:
                    # Marks the session as in use, at most every few minutes:
                    # the condition makes the other lookups a no-op update.
                    cur.execute(
                        """
                        UPDATE sessions SET last_seen = CURRENT_TIMESTAMP
                        WHERE token_hash = %s
                          AND (last_seen IS NULL OR last_seen
                               < CURRENT_TIMESTAMP - make_interval(secs => %s))
                        """,
                        (_digest(token), TOUCH_SECONDS),
                    )
            conn.commit()

        return row[0] if row else None

    def delete(self, token: str) -> None:
        if not token:
            return

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM sessions WHERE token_hash = %s",
                    (_digest(token),),
                )
            conn.commit()

    def delete_for_user(self, user_id: int) -> int:
        """Remove every session of a user; how many were removed.

        This is how a leaked session is revoked: there is no lookup by token
        anywhere, so ending them all is the only story that is simple enough
        to be trusted.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM sessions WHERE user_id = %s",
                    (user_id,),
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted

    def active(self, minutes: int = ACTIVE_MINUTES) -> list[ActiveSession]:
        """Sessions used within the last `minutes`, most recently used first.

        Not every unexpired session: a cookie lives for weeks after its tab is
        closed, and listing those would show sign-ins nobody is using.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.id, u.username, s.created_at, s.expires_at, s.agent,
                           s.token_hash, COALESCE(s.last_seen, s.created_at) AS seen
                    FROM sessions s JOIN users u ON u.id = s.user_id
                    WHERE s.expires_at > CURRENT_TIMESTAMP
                      AND COALESCE(s.last_seen, s.created_at)
                          > CURRENT_TIMESTAMP - make_interval(mins => %s)
                    ORDER BY seen DESC
                    """,
                    (minutes,),
                )
                rows = cur.fetchall()

        return [ActiveSession(*row) for row in rows]

    def delete_by_id(self, session_id: int) -> bool:
        """End one session by its row id, as the admin area names it."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
                deleted = cur.rowcount
            conn.commit()

        return deleted > 0

    def delete_expired(self) -> int:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM sessions
                    WHERE expires_at <= CURRENT_TIMESTAMP
                    """
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def digest(token: str) -> str:
    """The stored form of a cookie value, to recognise the caller's own row."""
    return _digest(token)
