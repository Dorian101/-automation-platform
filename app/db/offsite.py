"""A copy of the nightly dump off the host, sent to the administrators' chats.

The dumps sit on the same disk as the database, so a lost server takes the
backups with it. The bot already reaches the owner, a dump is a few hundred
kilobytes against a 50 MB limit for a bot's file, and the chat keeps every copy
with its date — no other account, key or dependency.

The dump holds notes, expenses and password hashes, and a chat is not
end-to-end encrypted, so the file is sealed first: symmetric gpg with a
passphrase from `BACKUP_PASSPHRASE`. Restore with `gpg -d <file> > dump.sql`.
"""

import asyncio
import subprocess
import tempfile
from pathlib import Path

from aiogram.types import FSInputFile

from app.bot.bot import create_bot


def encrypt(source: Path, passphrase: str, target_dir: Path) -> Path:
    """Seal `source` into `<target_dir>/<name>.gpg` and return that path.

    gpg gets a throwaway home in the target directory: the backup unit has no
    writable home, and a keyring is not needed for a passphrase. The
    passphrase goes through stdin, never the command line, where any process
    on the host could read it.
    """
    target = target_dir / (source.name + ".gpg")
    home = target_dir / "gnupg"
    home.mkdir(mode=0o700)

    result = subprocess.run(
        [
            "gpg", "--batch", "--yes", "--homedir", str(home),
            "--pinentry-mode", "loopback", "--passphrase-fd", "0",
            "--no-symkey-cache", "--symmetric", "--cipher-algo", "AES256",
            "--output", str(target), str(source),
        ],
        input=passphrase.encode(),
        capture_output=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "gpg failed: " + result.stderr.decode(errors="replace").strip()
        )

    return target


async def _send(token: str, chats: list[int], path: Path) -> None:
    bot = create_bot(token)

    try:
        for chat in chats:
            await bot.send_document(
                chat, FSInputFile(path), caption=f"Бэкап базы: {path.name}"
            )
    finally:
        await bot.session.close()


def send_copy(
    dump: Path, passphrase: str, token: str, chats: list[int], send=None
) -> Path:
    """Encrypt the dump and send it to every chat. Returns the sent file's name.

    Nothing encrypted stays behind: the sealed copy lives in a temporary
    directory for as long as the send takes. `send` is for tests.
    """
    if not chats:
        raise RuntimeError("no administrator has linked Telegram")

    with tempfile.TemporaryDirectory() as work:
        sealed = encrypt(dump, passphrase, Path(work))
        asyncio.run((send or _send)(token, chats, sealed))

        return Path(sealed.name)
