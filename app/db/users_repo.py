import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class User:
    id: int
    username: str
    is_active: bool


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
            raise ValueError("username must not be empty")

        if len(trimmed) > MAX_USERNAME_LENGTH:
            raise ValueError(
                f"username must be at most {MAX_USERNAME_LENGTH} characters"
            )

        # fullmatch on a non-empty string: the class demands at least one
        # character, which the emptiness check above has already guaranteed.
        if _USERNAME_RE.fullmatch(trimmed) is None:
            raise ValueError(
                "username may only contain letters, digits, dots, dashes "
                "and underscores"
            )

        password_hash = hash_password(password)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash)
                    VALUES (%s, %s)
                    RETURNING id, username, is_active
                    """,
                    (trimmed, password_hash),
                )
                row = cur.fetchone()
            conn.commit()

        return User(id=row[0], username=row[1], is_active=row[2])

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
                    SELECT id, username, is_active
                    FROM users
                    ORDER BY lower(username)
                    """,
                )
                rows = cur.fetchall()

        return [User(id=row[0], username=row[1], is_active=row[2]) for row in rows]

    def get_by_id(self, user_id: int) -> User | None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, is_active
                    FROM users
                    WHERE id = %s
                    """,
                    (user_id,),
                )
                row = cur.fetchone()

        return self._to_user(row)

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
                    SELECT id, username, is_active
                    FROM users
                    WHERE lower(username) = lower(%s)
                    """,
                    (username.strip(),),
                )
                row = cur.fetchone()

        return self._to_user(row)

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

    @staticmethod
    def _to_user(row) -> User | None:
        if row is None:
            return None

        return User(id=row[0], username=row[1], is_active=row[2])


def _dummy_hash() -> str:
    global _DUMMY_HASH

    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("a-real-password-that-nobody-uses")

    return _DUMMY_HASH
