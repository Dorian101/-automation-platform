import asyncio
import json
import re
from collections.abc import Iterable

from aiohttp import web

from app.core.identity import Identity, ids_of

MAX_PER_PERSON = 20
MAX_TOTAL = 500
HEARTBEAT_SECONDS = 25
QUEUE_SIZE = 32
TAB = re.compile(r"[A-Za-z0-9-]{1,64}")
CLOSE = object()


def tab_of(request: web.Request) -> str:
    value = request.headers.get("X-Tab", "")
    return value if TAB.fullmatch(value) else ""


class LiveHub:
    def __init__(self, persons):
        self._persons = persons
        self._queues: dict[str, set[asyncio.Queue]] = {}
        self._admins: set[asyncio.Queue] = set()
        self._closed = False

    def _keys(self, identity: Identity) -> set[str]:
        return set(ids_of(self._persons.identities(identity)))

    def subscribe(
        self, identity: Identity, admin: bool = False
    ) -> asyncio.Queue | None:
        if self._closed:
            return None

        key = str(identity)
        mine = self._queues.setdefault(key, set())

        if len(mine) >= MAX_PER_PERSON or self.count() >= MAX_TOTAL:
            if not mine:
                del self._queues[key]
            return None

        queue = asyncio.Queue(QUEUE_SIZE)
        mine.add(queue)
        if admin:
            self._admins.add(queue)
        return queue

    def unsubscribe(self, identity: Identity, queue: asyncio.Queue) -> None:
        key = str(identity)
        mine = self._queues.get(key)
        self._admins.discard(queue)

        if mine is None:
            return

        mine.discard(queue)

        if not mine:
            del self._queues[key]

    def count(self) -> int:
        return sum(len(queues) for queues in self._queues.values())

    def changed(self, identity: Identity, page: str, origin: str = "") -> None:
        if not self._queues:
            return

        message = {"page": page, "origin": origin}

        for key in self._keys(identity):
            _offer(self._queues.get(key, ()), message)

    def admin_changed(self, *pages: str) -> None:
        for page in pages:
            _offer(self._admins, {"page": page, "origin": ""})

    def close(self) -> None:
        self._closed = True
        for queues in self._queues.values():
            for queue in queues:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(CLOSE)


def _offer(queues: Iterable[asyncio.Queue], message: dict) -> None:
    for queue in queues:
        if not queue.full():
            queue.put_nowait(message)


async def stream(
    request: web.Request,
    hub: LiveHub,
    identity: Identity,
    still_signed_in,
    admin: bool = False,
) -> web.StreamResponse:
    queue = hub.subscribe(identity, admin)

    if queue is None:
        return web.json_response({"error": "Too many connections"}, status=429)

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store, no-transform",
            "X-Accel-Buffering": "no",
        }
    )

    try:
        await response.prepare(request)
        await response.write(b"retry: 5000\nevent: ping\ndata: {}\n\n")

        while True:
            try:
                message = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                if not still_signed_in():
                    break
                await response.write(b"event: ping\ndata: {}\n\n")
                continue

            if message is CLOSE or not still_signed_in():
                break

            await response.write(
                f"event: change\ndata: {json.dumps(message)}\n\n".encode()
            )
    except ConnectionResetError:
        pass
    finally:
        hub.unsubscribe(identity, queue)

    return response


def notify_all(hub: LiveHub, plugins: Iterable) -> None:
    for plugin in plugins:
        if plugin.page:
            plugin.notify_change = hub.changed
