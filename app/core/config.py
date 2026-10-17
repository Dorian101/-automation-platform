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

    # Attempts per address per window, counted across every username.
    #
    # The per-user window above cannot carry this weight. Its key is the address
    # *and* the name, which is deliberate so that one person's typos do not lock
    # out a whole office behind one proxy — but it also means a fresh name is a
    # fresh allowance, so an address that varies the name is never limited at
    # all. Since a login costs a scrypt at N=2^17, roughly 0.26 s of blocked
    # event loop on this hardware, an unlimited number of names is an unlimited
    # number of seconds the whole site stops answering.
    #
    # Sized above the per-user window rather than equal to it, for the same
    # reason that window carries the name: several people legitimately share one
    # address, and each of them gets their own allowance inside this one.
    LOGIN_ATTEMPTS_PER_ADDRESS_PER_WINDOW = int(
        os.getenv("LOGIN_ATTEMPTS_PER_ADDRESS_PER_WINDOW", "20")
    )
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

    # Addresses whose requests leave no row in the visit log, comma separated.
    # The owner reads that log to find out whether anybody else came, and their
    # own checking is otherwise indistinguishable from a stranger's, because no
    # address is ever stored and so none is on the row to recognise. Comparing
    # the address and then discarding it is the one thing that does not have to
    # be kept: this is a filter on the way in, not a column. Empty records
    # everything, which is what a deployment with no owner traffic needs.
    VISIT_IGNORE_IPS = frozenset(
        address.strip()
        for address in os.getenv("VISIT_IGNORE_IPS", "").split(",")
        if address.strip()
    )

    # The secret a request carries to be left out of the visit log, sent as
    # X-Platform-Probe. An address alone cannot do this job: it is written on
    # the machine the owner browses from, so it stops matching the moment that
    # address changes, and it says nothing about a request made from anywhere
    # else — including measuring, which is not the owner at all and arrives
    # from every address in turn. A secret travels with the request instead of
    # with the caller.
    #
    # This is not a way to keep a visitor out of the log. Anyone holding it can
    # hide, and a header is a poor secret against someone determined. What it
    # is for is keeping the owner's own working out of a count meant to answer
    # one question: was anybody here. Empty trusts nothing, which is what a
    # deployment that never sets it should mean.
    VISIT_PROBE_TOKEN = os.getenv("VISIT_PROBE_TOKEN", "").strip()
