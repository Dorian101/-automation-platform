"""Administrative commands for web accounts and backups.

    python -m app.manage create-user <username>
    python -m app.manage list-users
    python -m app.manage link-telegram <username> <chat_id>
    python -m app.manage unlink-telegram <username>
    python -m app.manage list-links
    python -m app.manage backup

Accounts are created here from the machine, with no browser involved, which
is what an administrator wants when an invite code should never be typed
anywhere. The browser alternative is the sign-up page, and that only exists
when SIGNUP_INVITE_CODE is configured — an invite code rather than an open
form is what keeps a private tool from becoming an open invitation. The
password is never accepted as a command line argument, because arguments are
visible to any other user on the host and end up in shell history.

`link-telegram` is the way out when the browser cannot reach Telegram: the
Telegram account is the only other way in, so a wrong or revoked link would
otherwise be a dead end with no way back from the host.

`backup` is what the systemd timer calls. It is a plain command so that a
backup does not depend on the application being able to start.
"""

import argparse
import getpass
import logging
import sys

from psycopg.errors import UniqueViolation

from app.core.config import Config
from app.core.passwords import PasswordError
from app.db.backup import BackupManager
from app.db.database import Database
from app.db.links_repo import TelegramLinksRepository
from app.db.migrations import MigrationRunner
from app.db.users_repo import UsersRepository

# Audit trail for the significant account actions the CLI performs, using the
# same basicConfig that main() installs, so the lines show up in the journal
# next to the web interface's.
logger = logging.getLogger("manage")


def create_user(args: argparse.Namespace) -> int:
    users = _ready_database()

    password = _read_password()

    try:
        user = users.create(args.username, password)
    except PasswordError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except UniqueViolation:
        print(f"error: user '{args.username}' already exists", file=sys.stderr)
        return 1

    print(f"Created user '{user.username}'")
    logger.info("Created web account %r", user.username)
    return 0


def list_users(args: argparse.Namespace) -> int:
    users = _ready_database()
    accounts = users.list_all()

    if not accounts:
        print("No users. Create one with: python -m app.manage create-user <username>")
        return 0

    for account in accounts:
        state = "active" if account.is_active else "disabled"
        print(f"{account.username}\t{state}")

    return 0


def link_telegram(args: argparse.Namespace) -> int:
    """Pair a web account with a Telegram chat, by hand."""
    links, users = _ready_links()

    account = users.get_by_username(args.username)

    if account is None:
        print(f"error: no user '{args.username}'", file=sys.stderr)
        return 1

    existing = links.get_by_user_id(account.id)
    chat_id = _chat_id(args.chat_id)

    if existing is not None:
        if existing.telegram_id == chat_id:
            print(f"{account.username} is already linked to {chat_id}")
            return 0

        # Not overwritten silently. The previous chat keeps whatever data it
        # already has, and an account that was linked by mistake is exactly the
        # case where guessing would be worst.
        links.unlink(account.id)
        print(
            f"Replaced link: {account.username} was {existing.telegram_id}, "
            f"now {chat_id}",
        )
        return _relink(links, account.id, chat_id, account.username)

    return _relink(links, account.id, chat_id, account.username)


def _relink(
    links: TelegramLinksRepository,
    user_id: int,
    chat_id: int,
    name: str,
) -> int:
    try:
        links.link(user_id, chat_id)
    except UniqueViolation:
        owner = links.get_by_telegram_id(chat_id)

        # Which of the two conflicts happened is worth spelling out: the same
        # error comes out of the database for both, and "cannot link" on its own
        # sends someone looking in the wrong place.
        if owner is not None:
            print(
                f"error: chat {chat_id} is already linked to another account",
                file=sys.stderr,
            )
        else:
            print(f"error: {name} is already linked", file=sys.stderr)

        return 1

    print(f"Linked {name} to Telegram chat {chat_id}")
    logger.info("Linked web account %s to Telegram chat %s", name, chat_id)
    return 0


def unlink_telegram(args: argparse.Namespace) -> int:
    links, users = _ready_links()

    account = users.get_by_username(args.username)

    if account is None:
        print(f"error: no user '{args.username}'", file=sys.stderr)
        return 1

    if not links.unlink(account.id):
        print(f"{account.username} is not linked to a Telegram account")
        return 0

    print(f"Unlinked {account.username}")
    logger.info("Unlinked web account %s", account.username)
    return 0


def list_links(args: argparse.Namespace) -> int:
    links, users = _ready_links()
    pairs = links.list_all()

    if not pairs:
        print("No Telegram links. Create one with: "
              "python -m app.manage link-telegram <username> <chat_id>")
        return 0

    for pair in pairs:
        account = users.get_by_id(pair.user_id)
        name = account.username if account else f"user #{pair.user_id}"

        print(f"{name}\t{pair.telegram_id}\t{pair.linked_at}")

    return 0


def _chat_id(raw: str) -> int:
    """Read a chat id, refusing anything that is not one.

    Stored as BIGINT, so a value that cannot be that would reach the driver as
    an error at insert time instead of here.
    """
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"chat id must be a number, not {raw!r}") from None


def backup(args: argparse.Namespace) -> int:
    """Write one backup and drop the ones that have aged out.

    Deliberately does not apply migrations or touch the schema: a backup has
    to work exactly when the application is in a bad state.
    """
    manager = BackupManager()

    path = manager.create_backup("scheduled")
    print(f"Backup written: {path}")

    removed = manager.prune(Config.BACKUP_RETENTION_DAYS)

    if removed:
        print(f"Pruned {len(removed)} expired backup(s)")
    elif Config.BACKUP_RETENTION_DAYS > 0:
        print(f"Retention {Config.BACKUP_RETENTION_DAYS}d, nothing to prune")

    return 0


def _ready_database() -> UsersRepository:
    """Apply pending migrations so the command works on a fresh install."""
    database = Database()
    MigrationRunner(database).run()
    return UsersRepository(database)


def _ready_links() -> tuple[TelegramLinksRepository, UsersRepository]:
    """The same, for the commands that need accounts and links together."""
    database = Database()
    MigrationRunner(database).run()
    return TelegramLinksRepository(database), UsersRepository(database)


def _read_password() -> str:
    """Read the password from a prompt, or from stdin when not a terminal."""
    if sys.stdin.isatty():
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Confirm password: ")

        if first != second:
            raise ValueError("passwords do not match")

        return first

    line = sys.stdin.readline().rstrip("\n")

    if not line:
        raise ValueError("no password on standard input")

    return line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.manage",
        description="Manage web accounts.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-user", help="Create a web account")
    create.add_argument("username", help="Account name, matched case-insensitively")
    create.set_defaults(func=create_user)

    listing = subparsers.add_parser("list-users", help="List web accounts")
    listing.set_defaults(func=list_users)

    link = subparsers.add_parser(
        "link-telegram",
        help="Pair a web account with a Telegram chat",
    )
    link.add_argument("username", help="Account name, matched case-insensitively")
    link.add_argument(
        "chat_id",
        help="Telegram chat id, negative for a group",
    )
    link.set_defaults(func=link_telegram)

    unlink = subparsers.add_parser(
        "unlink-telegram",
        help="Remove a web account's Telegram link",
    )
    unlink.add_argument("username", help="Account name, matched case-insensitively")
    unlink.set_defaults(func=unlink_telegram)

    links_listing = subparsers.add_parser(
        "list-links",
        help="List web accounts and their Telegram chats",
    )
    links_listing.set_defaults(func=list_links)

    backuping = subparsers.add_parser(
        "backup",
        help="Write a database backup and prune expired ones",
    )
    backuping.set_defaults(func=backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    args = build_parser().parse_args(argv)

    try:
        return args.func(args)
    except (ValueError, RuntimeError) as error:
        # RuntimeError is how BackupManager reports a failed pg_dump. A
        # traceback in the journal would say the same thing less clearly, and
        # a non-zero exit is what makes the systemd unit report a failure.
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
