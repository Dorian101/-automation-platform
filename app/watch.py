import asyncio
import logging
import ssl
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

from app.core.config import Config
from app.web.system import disk

logger = logging.getLogger(__name__)

CERT_DAYS = 14
DOMAIN_DAYS = 30
DISK_SHARE = 0.8
CHECK_AT = time(9, 0)
TIMEOUT_SECONDS = 15

LAST: dict[str, str] = {}


async def certificate_expiry(host: str) -> datetime:
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(
            host, 443, ssl=ssl.create_default_context(), server_hostname=host
        ),
        TIMEOUT_SECONDS,
    )
    certificate = writer.get_extra_info("peercert")
    writer.close()
    await writer.wait_closed()
    return datetime.fromtimestamp(
        ssl.cert_time_to_seconds(certificate["notAfter"]), UTC
    )


async def domain_expiry(host: str) -> datetime:
    domain = ".".join(host.split(".")[-2:])
    async with ClientSession(timeout=ClientTimeout(total=TIMEOUT_SECONDS)) as http:
        async with http.get(f"https://rdap.org/domain/{domain}") as response:
            response.raise_for_status()
            data = await response.json(content_type=None)

    for event in data.get("events", []):
        if event.get("eventAction") == "expiration":
            return datetime.fromisoformat(event["eventDate"].replace("Z", "+00:00"))

    raise ValueError("no expiration in the RDAP answer")


def disk_share() -> float:
    root = disk()["root"]
    return root["used"] / root["total"] if root["total"] else 0.0


async def problems(host: str, now: datetime) -> list[str]:
    found = []
    LAST["checked"] = now.astimezone(ZoneInfo(Config.DISPLAY_TIMEZONE)).strftime(
        "%d.%m.%Y %H:%M"
    )

    for key, label, fetch, limit in (
        ("cert", "Сертификат", certificate_expiry, CERT_DAYS),
        ("domain", "Домен", domain_expiry, DOMAIN_DAYS),
    ):
        try:
            expires = await fetch(host)
            days = (expires - now).days
        except Exception as error:
            logger.warning("Could not check the %s expiry: %s", key, error)
            LAST[key] = "не проверено"
            if key == "cert":
                found.append(f"{label} {host} не удалось проверить: {error}")
            continue

        LAST[key] = f"{expires:%d.%m.%Y}, через {days} дн."
        if days < limit:
            found.append(f"{label} {host} истекает через {days} дн.")

    share = disk_share()
    if share >= DISK_SHARE:
        found.append(f"Диск занят на {share:.0%}")

    return found


def next_check(now: datetime) -> datetime:
    zone = ZoneInfo(Config.DISPLAY_TIMEZONE)
    local = now.astimezone(zone)
    planned = datetime.combine(local.date(), CHECK_AT, zone)
    if planned <= local:
        planned += timedelta(days=1)
    return planned


class Watch:
    def __init__(self, notifier, users, host: str):
        self._notifier = notifier
        self._users = users
        self._host = host

    async def run(self) -> None:
        if not self._host:
            return

        await self._check(notify=False)
        planned = next_check(datetime.now(UTC))

        while True:
            await asyncio.sleep(max(0, (planned - datetime.now(UTC)).total_seconds()))
            await self._check(notify=True)
            planned = next_check(planned + timedelta(minutes=1))

    async def _check(self, notify: bool) -> None:
        try:
            found = await problems(self._host, datetime.now(UTC))
            if found and notify:
                await self._tell_admins("\n".join(found))
        except Exception:
            logger.exception("Expiry and disk check failed")

    async def _tell_admins(self, text: str) -> None:
        for user in self._users.active_admins():
            try:
                await self._notifier.deliver(self._users.identity_for(user), text)
            except Exception:
                logger.exception("Watch warning could not reach %s", user.username)
