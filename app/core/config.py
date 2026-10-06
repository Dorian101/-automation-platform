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
