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
