"""Password hashing.

scrypt is used because it is in the standard library, ships with a per-password
salt and requires memory, which is what makes brute force expensive. Adding a
dependency for this would buy no security and cost an install surface.

Stored format: ``scrypt$<n>$<r>$<p>$<salt>$<hash>``, all base64. Parameters are
recorded per password so that raising the cost later does not invalidate
existing hashes.
"""

import base64
import hashlib
import hmac
import secrets

# OWASP recommends at least 2^17 within scrypt's non-interactive guidance.
# The parameter is recorded per hash, so hashes made at the old cost keep
# verifying after this is raised.
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32
SALT_BYTES = 16

ALGORITHM = "scrypt"

# 64 is long enough to resist guessing and short enough not to annoy anyone.
MIN_PASSWORD_LENGTH = 8


class PasswordError(ValueError):
    """Raised when a password cannot be accepted for creation."""


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )

    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)

    return "$".join(
        (
            ALGORITHM,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _encode(salt),
            _encode(digest),
        )
    )


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash.

    Returns False for malformed input rather than raising: a corrupt or
    truncated hash must not turn a failed login into a 500.
    """
    try:
        algorithm, n, r, p, salt, expected = stored.split("$")
    except ValueError:
        return False

    if algorithm != ALGORITHM:
        return False

    try:
        params = (int(n), int(r), int(p))
        salt_bytes = _decode(salt)
        expected_bytes = _decode(expected)
    except (ValueError, TypeError):
        return False

    if len(salt_bytes) < 8 or len(expected_bytes) < 8:
        return False

    try:
        actual = _derive(password, salt_bytes, *params)
    except (ValueError, MemoryError):
        return False

    return hmac.compare_digest(actual, expected_bytes)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=DKLEN,
        # The default 32 MiB is below what 2^17 demands (128 MiB of working
        # memory), and the error message scrypt raises for that is cryptic.
        maxmem=256 * 1024 * 1024,
    )


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value)
