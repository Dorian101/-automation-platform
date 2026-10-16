import os

from dotenv import load_dotenv

# Class attributes below are evaluated at import time. If the entry point loads
# .env after importing this module, every value silently falls back to its
# default. Loading here makes the order irrelevant.
# Does not override variables already present in the environment.
load_dotenv()


class Config:
    APP_NAME = os.getenv("APP_NAME", "automation-platform")
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"

    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = int(os.getenv("DB_PORT", "5432"))
    DB_NAME = os.getenv("DB_NAME", "automation_platform")
    DB_USER = os.getenv("DB_USER", "automation")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")

    WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
    WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

    # Shared secret required in the X-Platform-Auth header. Empty disables the
    # check, which is only appropriate for local development.
    WEB_ACCESS_TOKEN = os.getenv("WEB_ACCESS_TOKEN", "")

    # Secret the session-backed CSRF tokens are signed with. It has to outlive
    # the process, or every form open across a restart — a page held open
    # while a deploy goes out, most likely — comes back "the form has expired"
    # with nothing the reader did wrong. Separate from WEB_ACCESS_TOKEN
    # rather than borrowed from it: one is a credential the proxy holds, the
    # other is not, and a leak of either should not hand over both.
    #
    # Empty falls back to a random value per process, which is the old
    # behaviour and the reason a tab that predates a deploy stops working.
    # The runbook has the command that sets it.
    CSRF_SECRET = os.getenv("CSRF_SECRET", "")

    SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))

    # Invite code required to register a web account. Empty disables sign-up
    # entirely: the login page then offers no link and /signup answers 404,
    # which is what every deployment that never set this should see. It should
    # be long and random — the code is the only thing standing between the
    # internet and an account: openssl rand -hex 16
    SIGNUP_INVITE_CODE = os.getenv("SIGNUP_INVITE_CODE", "")

    # Telegram username of the bot, without the leading @. The Login Widget
    # needs it, and so does the button on the account page. Empty hides the
    # whole linking flow, which is what a deployment that never registered a
    # domain with BotFather should see.
    TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")

    # Telegram bot token. Read here as well as in main.py because verifying a
    # Login Widget signature needs it, and a second os.getenv would be a second
    # place for the two to disagree.
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")

    # How long a Login Widget payload stays acceptable. A valid signature never
    # stops being valid, so without a limit one captured anywhere would be
    # replayable years later. Five minutes is generous for a redirect that
    # follows the click immediately.
    TELEGRAM_LOGIN_MAX_AGE_SECONDS = int(
        os.getenv("TELEGRAM_LOGIN_MAX_AGE_SECONDS", "300")
    )

    # Sliding-window rate limits on the public forms. They run per process and
    # the deployment runs a single web process, so there is nowhere else for
    # two windows to disagree. 0 disables the limit, which is only sensible on
    # a deployment that is itself behind a limiting edge.
    RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    LOGIN_ATTEMPTS_PER_WINDOW = int(os.getenv("LOGIN_ATTEMPTS_PER_WINDOW", "5"))
    SIGNUP_ATTEMPTS_PER_WINDOW = int(os.getenv("SIGNUP_ATTEMPTS_PER_WINDOW", "10"))
    SIGNUP_WINDOW_SECONDS = int(os.getenv("SIGNUP_WINDOW_SECONDS", "3600"))

    # The public demonstration, which writes on every page load rather than
    # only on a submit: an empty guest namespace is seeded on arrival, so a
    # request that creates a fresh cookie costs about two dozen rows. The caps
    # inside a plugin are per namespace, and a namespace is defined by a
    # caller-controlled cookie, so without a limit on the request rate the
    # allowance is per visit rather than per visitor.
    #
    # Counted per address and over an hour rather than per minute: a person
    # reads a few pages and enters a couple of rows, while the number that
    # matters is how many namespaces one address can fill between cleanups.
    # 200 is roughly an order of magnitude above a real visit, which bounds a
    # single address to a few thousand rows an hour.
    DEMO_ATTEMPTS_PER_WINDOW = int(os.getenv("DEMO_ATTEMPTS_PER_WINDOW", "200"))
    DEMO_WINDOW_SECONDS = int(os.getenv("DEMO_WINDOW_SECONDS", "3600"))

    # Whether the session cookie carries the Secure flag. This cannot be
    # inferred from the request: the app talks to the reverse proxy over plain
    # HTTP on loopback, so the socket is never TLS and request.secure is always
    # false even in production. Keep this on everywhere except local http://.
    SESSION_COOKIE_SECURE = (
        os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
    )

    # Where scheduled backups are written. Empty falls back to <repo>/backups,
    # which is only appropriate for local development: on a server this should
    # point outside the working tree so a stray `git clean` cannot take the
    # backups with it.
    BACKUP_DIR = os.getenv("BACKUP_DIR", "")

    # Backups older than this are deleted after each run. 0 disables pruning
    # and keeps everything.
    BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "14"))

    # Time zone the administration pages render timestamps in. Rows are written
    # by CURRENT_TIMESTAMP on a host that keeps UTC, and the column carries no
    # offset, so the stored value is UTC whatever this is set to — that is why
    # prune-visits can compare against datetime.now() and stay correct. This
    # only decides what a person reads. Any IANA name ZoneInfo accepts; empty
    # means UTC.
    DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "UTC") or "UTC"
