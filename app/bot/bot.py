import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession


def create_bot(token: str) -> Bot:
    proxy_url = os.getenv("PROXY_URL")

    if proxy_url:
        session = AiohttpSession(proxy=proxy_url)
        return Bot(token=token, session=session)

    return Bot(token=token)


def create_dispatcher() -> Dispatcher:
    return Dispatcher()
