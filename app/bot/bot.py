import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage


def create_bot(token: str) -> Bot:
    proxy_url = os.getenv("PROXY_URL")

    if proxy_url:
        session = AiohttpSession(proxy=proxy_url)
        return Bot(token=token, session=session)

    return Bot(token=token)


def create_dispatcher() -> Dispatcher:
    # The FSM behind the button input flow needs somewhere to keep its state,
    # and in-memory is the right size for a personal bot: the state is only
    # "which step of a form is this reader on", worth nothing across a restart.
    return Dispatcher(storage=MemoryStorage())
