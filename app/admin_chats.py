"""Reaching the administrators in Telegram from a command on the host.

The running bot is not involved: a scheduled job is its own process, so it
opens its own short session with the same token. Sending is all it does, which
does not disturb the bot's polling.
"""

from app.bot.bot import create_bot
from app.db.links_repo import TelegramLinksRepository
from app.db.users_repo import UsersRepository

#: Telegram's limit for one message.
MESSAGE_LIMIT = 4096


def recipients(users: UsersRepository, links: TelegramLinksRepository) -> list[int]:
    """The chats of active administrators who linked Telegram."""
    chats = []

    for user in users.active_admins():
        link = links.get_by_user_id(user.id)

        if link is not None:
            chats.append(link.telegram_id)

    return chats


async def send_text(token: str, chats: list[int], text: str) -> None:
    """Send plain text to every chat, cut to what one message holds."""
    bot = create_bot(token)

    try:
        for chat in chats:
            await bot.send_message(chat, text[:MESSAGE_LIMIT])
    finally:
        await bot.session.close()
