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

    # Whether the session cookie carries the Secure flag. This cannot be
    # inferred from the request: the app talks to the reverse proxy over plain
    # HTTP on loopback, so the socket is never TLS and request.secure is always
    # false even in production. Keep this on everywhere except local http://.
    SESSION_COOKIE_SECURE = (
        os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
    )
