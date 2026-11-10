import base64
import binascii
import re
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec

from app.core.identity import WEB, Identity

from .database import Database

MAX_DEVICES = 10
ENDPOINT = re.compile(
    r"https://([a-z0-9-]+\.)+[a-z]{2,}(:443)?/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*"
)
PUSH_HOSTS = (
    "push.apple.com",
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
)


class BadSubscription(ValueError):
    pass


class TooManyDevices(Exception):
    pass


def _decoded(value) -> bytes:
    if not isinstance(value, str) or len(value) > 200:
        return b""

    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return b""


def _point(raw: bytes) -> bool:
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        return False

    return True


def checked(subscription) -> tuple[str, str, str]:
    if not isinstance(subscription, dict):
        raise BadSubscription("subscription")

    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")

    if (
        not isinstance(endpoint, str)
        or len(endpoint) > 1000
        or not isinstance(keys, dict)
    ):
        raise BadSubscription("endpoint")

    if not ENDPOINT.fullmatch(endpoint) or "@" in endpoint.split("/", 3)[2]:
        raise BadSubscription("endpoint")

    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise BadSubscription("endpoint") from None

    host = parts.hostname or ""

    if (
        parts.scheme != "https"
        or port not in (None, 443)
        or parts.username
        or parts.password
        or not any(host == known or host.endswith("." + known) for known in PUSH_HOSTS)
    ):
        raise BadSubscription("endpoint")

    p256dh, auth = keys.get("p256dh"), keys.get("auth")

    if not _point(_decoded(p256dh)) or len(_decoded(auth)) != 16:
        raise BadSubscription("keys")

    return endpoint, p256dh, auth


class PushRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(self, user_id: int, subscription, label: str, session: str = "") -> bool:
        endpoint, p256dh, auth = checked(subscription)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
                cur.execute(
                    "SELECT count(*) FROM push_subscriptions "
                    "WHERE user_id = %s AND endpoint <> %s",
                    (user_id, endpoint),
                )

                if cur.fetchone()[0] >= MAX_DEVICES:
                    raise TooManyDevices

                cur.execute(
                    """
                    INSERT INTO push_subscriptions
                        (user_id, endpoint, p256dh, auth, label, session_hash)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (endpoint) DO UPDATE
                    SET user_id = EXCLUDED.user_id, p256dh = EXCLUDED.p256dh,
                        auth = EXCLUDED.auth, label = EXCLUDED.label,
                        session_hash = EXCLUDED.session_hash
                    RETURNING xmax = 0
                    """,
                    (user_id, endpoint, p256dh, auth, label[:80], session),
                )
                added = cur.fetchone()[0]
            conn.commit()

        return added

    def devices(self, user_id: int) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, label, created_at FROM push_subscriptions "
                    "WHERE user_id = %s ORDER BY id",
                    (user_id,),
                )
                return [
                    {"id": row[0], "label": row[1], "created_at": row[2]}
                    for row in cur.fetchall()
                ]

    def subscriptions(self, identity: Identity) -> list[dict]:
        if identity.kind != WEB:
            return []

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.endpoint, s.p256dh, s.auth
                    FROM push_subscriptions s JOIN users u ON u.id = s.user_id
                    WHERE lower(u.username) = lower(%s) AND u.is_active
                    """,
                    (identity.id,),
                )
                return [
                    {"endpoint": row[0], "keys": {"p256dh": row[1], "auth": row[2]}}
                    for row in cur.fetchall()
                ]

    def remove(self, user_id: int, device_id: int) -> bool:
        return self._delete("id = %s AND user_id = %s", (device_id, user_id))

    def remove_endpoint(self, user_id: int, endpoint: str) -> bool:
        return self._delete("endpoint = %s AND user_id = %s", (endpoint, user_id))

    def forget_session(self, session: str) -> None:
        if session:
            self._delete("session_hash = %s", (session,))

    def forget(self, endpoint: str) -> None:
        self._delete("endpoint = %s", (endpoint,))

    def _delete(self, condition: str, values: tuple) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM push_subscriptions WHERE {condition}", values)
                deleted = cur.rowcount
            conn.commit()

        return deleted > 0
