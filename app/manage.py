"""Administrative commands for web accounts and backups.

    python -m app.manage create-user <username>
    python -m app.manage list-users
    python -m app.manage backup

Accounts are created here from the machine, with no browser involved, which
is what an administrator wants when an invite code should never be typed
anywhere. The browser alternative is the sign-up page, and that only exists
when SIGNUP_INVITE_CODE is configured — an invite code rather than an open
form is what keeps a private tool from becoming an open invitation. The
password is never accepted as a command line argument, because arguments are
visible to any other user on the host and end up in shell history.

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
from app.db.migrations import MigrationRunner
from app.db.users_repo import UsersRepository


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
