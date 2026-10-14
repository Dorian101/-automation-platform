import re
from dataclasses import dataclass
from datetime import datetime

from app.core.identity import WEB, Identity
from app.core.passwords import hash_password, verify_password

from .database import Database

# Computed once, on the first failed lookup, and only then. Comparing against a
# real hash when the username is unknown keeps the response time of a miss close
# to that of a hit, so "user does not exist" cannot be told apart by timing.
_DUMMY_HASH: str | None = None

# Usernames are rejected up front, at the only place they enter the system.
# Besides keeping the profile tidy, the alphabet also bounds what can ever be
# written into a log line: an unrestricted name is an injection vector for
# forged log entries, and nobody needs a newline to feel identified.
_USERNAME_RE = re.compile(r"[A-Za-z0-9._-]+")

MAX_USERNAME_LENGTH = 32

#: How many accounts a search returns. A page, not a report.
SEARCH_LIMIT = 200


@dataclass(frozen=True)
class User:
    id: int
    username: str
    is_active: bool
    is_admin: bool
    created_at: datetime


class UsersRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def create(self, username: str, password: str) -> User:
        """Create a user and return them.

        Normalising to lowercase happens before storage so the unique index on
        the lowercased column can do its job, while username keeps whatever
        casing the caller supplied for display.
        """
        trimmed = username.strip()

        if not trimmed:
            raise ValueError("имя пользователя не может быть пустым")

        if len(trimmed) > MAX_USERNAME_LENGTH:
            raise ValueError(
                f"имя пользователя длиннее {MAX_USERNAME_LENGTH} символов"
            )

        # fullmatch on a non-empty string: the class demands at least one
        # character, which the emptiness check above has already guaranteed.
        if _USERNAME_RE.fullmatch(trimmed) is None:
            raise ValueError(
                "имя пользователя может содержать только латинские буквы, "
                "цифры, точки, дефисы и подчёркивания"
            )

        password_hash = hash_password(password)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash)
                    VALUES (%s, %s)
                    RETURNING id, username, is_active, is_admin, created_at
                    """,
                    (trimmed, password_hash),
                )
                row = cur.fetchone()
            conn.commit()

        return _user(row)

    def count(self) -> int:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM users")
                return cur.fetchone()[0]

    def list_all(self) -> list[User]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, is_active, is_admin, created_at
                    FROM users
                    ORDER BY lower(username)
                    """,
                )
                rows = cur.fetchall()

        return [
            _user(row)
            for row in rows
        ]

    def get_by_id(self, user_id: int) -> User | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, is_active, is_admin, created_at
                    FROM users
                    WHERE id = %s
                    """,
                    (user_id,),
                )
                row = cur.fetchone()

        return _user(row) if row is not None else None

    def authenticate(self, username: str, password: str) -> User | None:
        """Return the user if the credentials match, otherwise None.

        Every branch costs one password derivation. An unknown username is
        checked against a throwaway hash, and an inactive account still gets
        verified before being rejected, so neither the existence of an account
        nor its state shows up in the response time.
        """
        user = self.get_by_username(username)
        stored = self._password_hash(user.id) if user else None

        if not verify_password(password, stored or _dummy_hash()):
            return None

        if user is None or not user.is_active:
            return None

        return user

    def get_by_username(self, username: str) -> User | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, is_active, is_admin, created_at
                    FROM users
                    WHERE lower(username) = lower(%s)
                    """,
                    (username.strip(),),
                )
                row = cur.fetchone()

        return _user(row) if row is not None else None

    def set_active(self, user_id: int, active: bool) -> bool:
        """Enable or disable an account; whether a row was changed."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET is_active = %s WHERE id = %s",
                    (active, user_id),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> list[User]:
        """Accounts whose name contains `query`, oldest first.

        `LIKE` and not `=`: an administrator is looking for a person by a
        fragment of what they typed into the invitation. The wildcards are
        escaped, so a query of `%` looks for a percent sign instead of
        quietly matching every account — a search box that answers differently
        depending on whether the query happens to contain a symbol is a thing
        nobody can reason about later.

        Capped because this is a page rather than a report: there is no
        question a person answers in the administration that needs every row
        of a table that could grow.
        """
        pattern = "%" + _escape_like(query.strip().lower()) + "%"

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, is_active, is_admin, created_at
                    FROM users
                    WHERE LOWER(username) LIKE %s
                    ORDER BY created_at, id
                    LIMIT %s
                    """,
                    (pattern, limit),
                )
                rows = cur.fetchall()

        return [_user(row) for row in rows]

    def set_admin(self, user_id: int, admin: bool) -> bool:
        """Grant or take back the administrative flag; whether a row changed.

        Separate from set_active because they answer different questions and
        an account can be either without being the other: disabled is about
        whether someone may sign in at all, administrative is about what they
        may do once they have.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET is_admin = %s WHERE id = %s",
                    (admin, user_id),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def set_password(self, user_id: int, password: str) -> None:
        """Replace an account's password.

        The password is validated by hash_password, so a short one raises
        PasswordError before any row is touched.
        """
        password_hash = hash_password(password)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (password_hash, user_id),
                )
            conn.commit()

    def identity_for(self, user: User) -> Identity:
        return Identity(kind=WEB, id=user.username)

    def _password_hash(self, user_id: int) -> str | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT password_hash FROM users WHERE id = %s",
                    (user_id,),
                )
                row = cur.fetchone()

        return row[0] if row else None


def _escape_like(value: str) -> str:
    """Make the LIKE wildcards in `value` mean themselves.

    The escape character is backslash and the escape is applied to the three
    characters LIKE treats specially, so a query cannot reach past itself.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _user(row) -> User:
    return User(
        id=row[0],
        username=row[1],
        is_active=row[2],
        is_admin=row[3],
        created_at=row[4],
    )


def _dummy_hash() -> str:
    global _DUMMY_HASH

    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("a-real-password-that-nobody-uses")

    return _DUMMY_HASH
