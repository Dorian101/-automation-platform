# Project State

## Current version
v0.17.0

## Project goal
Automation Platform is a personal automation system built around an extensible plugin architecture, reachable over Telegram and a web interface.

The project goal is to provide a modular platform where new automation features can be added as independent plugins without changing the core application, and work identically on every transport.

## Current status
The platform is operational.

## Implemented:

- Telegram bot integration
- Plugin architecture
- Persistent storage
- Background tasks inside plugins
- Several working automation plugins
- plugin lifecycle management
- lifecycle logging
- global error handling
- status monitoring
- PostgreSQL migrations system
- automatic schema migration execution on startup
- migration history tracking
- database indexes on user_id columns
- context managers for safe connection handling
- shared Database instance across plugins
- automated test suite
- ruff linter
- web interface as second entry point
- transport-namespaced user identity
- transport-independent plugin commands
- notification channel layer
- database health endpoint
- shared-secret access control on the web interface
- deployment runbook

## Current architecture

Entry points

Telegram (aiogram)          Web (aiohttp)
     │                           │
     └──────────┬────────────────┘
                ▼
        PluginManager
                │
                ├── Command dispatch
                ├── Plugin lifecycle
                └── Plugin management
                        │
                        ▼
                    Plugins
              execute() — transport-independent
                        │
                        ▼
               Repository layer
                        │
                        ▼
                 MigrationRunner
                        │
                        ▼
                   PostgreSQL

Notification delivery

Plugin → Notifier → resolver picks the target → channel by kind
                        │
              ┌─────────┴─────────┐
        TelegramChannel        WebChannel
                                (stub)


## Core components

Core

## Responsibilities:

* application startup
* configuration loading
* logging
* Telegram bot initialization
* web interface initialization
* notification channel registration


## Plugin system

## Implemented:

* BasePlugin contract
* PluginManager
* plugin registration
* plugin metadata
* command metadata
* plugin lifecycle
* transport-independent execute()
* automatic command routing

Plugins implement `execute(command, args, identity)` and return a
`CommandResult`. They contain no transport-specific code. The platform
dispatches declared commands on every transport automatically.

`router()` is optional and reserved for handlers a command cannot express,
such as inline keyboards or file uploads.

## Plugin lifecycle:

startup()
    ↓
plugin works
    ↓
shutdown()

Plugins manage their own resources through:

* on_startup()
* on_shutdown()

The core application does not know plugin internals.

## Current plugins

System

Purpose:

* platform commands

Implemented:

* /plugins
* /help
* /status

⸻

Notes

Purpose:

* personal notes storage

Implemented:

* create notes
* persistent storage
* PostgreSQL repository
* read across a linked Telegram pair, from either transport
* /notes numbers the list, /del removes one, /clear removes all
* notes are addressed by position, never by row id
* deletion spans a linked pair too, so a visible note is removable

⸻

Reminders

Purpose:

* scheduled notifications

Implemented:

* create reminders
* PostgreSQL storage
* background worker
* self-managed lifecycle
* delivered to the linked Telegram chat, not dropped in the browser
* an undeliverable reminder is given up on after three passes rather than
  retried for ever; a raising delivery is retried indefinitely

⸻

Clipboard

Purpose:

* personal temporary text storage

Implemented:

* /copy <text>
* /paste
* PostgreSQL repository
* one buffer across a linked pair — copy on the web, paste in the bot

⸻

## Web interface

Purpose:

* browser access to the same command set

Implemented:

* GET /login, POST /login — sign in form
* GET /signup, POST /signup — registration; 404 while SIGNUP_INVITE_CODE is unset
* POST /logout — ends the session
* GET / — HTML console
* GET /about, GET /project — static public documents, no session needed
* GET /api/commands — plugin and command metadata
* POST /api/command — command execution
* GET /health — database health check
* WEB_HOST / WEB_PORT configuration

Access control:

* reverse proxy terminates TLS and injects the X-Platform-Auth secret
* X-Platform-Auth shared secret, verified as middleware covering every route
* session cookie (HttpOnly, SameSite=Lax, Secure) issued at /login
* every route except the two auth pages, the two public documents (/about,
  /project) and /health requires a session
* WEB_HOST binds to loopback, port 8080 never exposed

An account can be paired with a Telegram chat, stored in `telegram_links` and
one-to-one in both directions. The pair never moves data: web rows stay under
`web:<name>`, Telegram rows stay under `telegram:<id>`, and neither identity is
merged into the other.

From the console, through Telegram's Login Widget: the page shows either a
link button or the linked chat with an unlink button, never both. Requires
`TELEGRAM_BOT_USERNAME` and a domain registered with `/setdomain`; without
both, no button and no route.

Implemented:
- GET /account/telegram/link — verifies the signed payload, binds it to the
  browser that started the flow with a one-use nonce, rejects payloads older
  than TELEGRAM_LOGIN_MAX_AGE_SECONDS
- POST /account/telegram/unlink — removes the pairing
- verification errors are deliberately vague; the reason is logged instead
- re-linking to a different chat replaces the old one and logs it; re-linking
  to the same one is a no-op rather than an error
- a chat already held by another account is refused and named

Managed from the host with `python -m app.manage link-telegram <username>
<chat_id>`, `unlink-telegram <username>` and `list-links`, which is also the
way out when the browser cannot reach Telegram.

The pair also widens reads. `PersonResolver.identities()` returns the identity
plus whatever it is paired with, and the notes and clipboard plugins pass that
set to their repositories:

* notes are read across the pair in both directions, so a note written in the
  bot appears in the console and vice versa, as one list in real order
* writes stay under the identity that wrote them — the stored row still
  records where it came from, and unlinking is a single delete rather than a
  row-by-row move back
* the clipboard is one buffer rather than a history, so `/paste` reads across
  the pair and the last write wins whoever made it

An identity with no pair resolves to itself, so an unpaired deployment behaves
exactly as it did before.

Accounts are created either from the host with `python -m app.manage
create-user`, or through the sign-up page when `SIGNUP_INVITE_CODE` is set.
Passwords are stored as salted scrypt hashes; only the SHA-256 of a session
token is kept. Two web users cannot read each other's data.

Identity resolution lives in app/web/auth.py and maps the session cookie to
`Identity(WEB, username)`. That function remains the seam for user accounts —
no plugin or repository knows about any of this.

Deployment runbook: docs/DEPLOYMENT.md

⸻

## Notifications

Purpose:

* deliver a message to a user over the transport they arrived on

Implemented:

* NotificationChannel interface
* TelegramChannel — sends through the bot
* WebChannel — placeholder that logs
* Notifier — routes by identity kind, and returns whether anything was sent
* LinkedChatResolver — a web account's notification is delivered to its paired
  Telegram chat; the only place where delivery crosses transports

⸻

## Database

Current database:

* PostgreSQL
* common database.py for all plugins
* SQL migrations support
* context managers for connection safety
* shared Database instance across all components
* indexes on user_id columns for all tables
* user_id is TEXT namespaced as kind:id

Database schema:

sql/migrations/
├── 001_initial.sql
├── 002_add_indexes.sql
├── 003_user_identity.sql
├── 004_users_sessions.sql
└── 005_telegram_links.sql

Pattern:

Plugin
  │
  ▼
Repository
  │
  ▼
PostgreSQL

## Current state:

* Existing repositories use shared database helper for PostgreSQL connection.
* All repositories use context managers to prevent connection leaks.
* Database schema changes are managed through SQL migrations.
* Migrations run automatically during application startup.
* Applied migrations are tracked in schema_migrations table.
* Migrations are wrapped in transactions with rollback on failure.
* Timestamps use UTC consistently (NOW() AT TIME ZONE 'UTC').

app/db/database.py

⸻

## Current limitations

* Sign-up is gated by one shared invite code with no approval step behind it —
  whoever holds the code can join — and there is still no password reset and
  no way to disable an account other than by hand in SQL.
* Web notifications are still dropped rather than delivered, but only for an
  account that has not linked Telegram. A linked one reaches its chat.
* Telegram linking needs a domain registered with BotFather, so it cannot be
  tried on localhost — the button stays hidden until that is done.
* /clear removes every note at once with no confirmation, which is right for a
  single-user tool and wrong the day there is a second one.
* No external integrations.
* Web console is functional but minimal.

⸻

## Potential tasks:

* add reminder to /cancel and /reminders listing;
* call BackupManager before applying a migration, so a failed deploy has a
  dump from minutes ago rather than from last night;
* manage accounts beyond create and list — disable, rename, reset a password,
  and revoke a session that has leaked.

⸻

## Development rules

* Keep core independent from plugin internals.
* New functionality should be implemented as plugins.
* Keep business logic transport-independent.
* Avoid unnecessary architectural complexity.
* Prefer incremental improvements over large rewrites.

## Development workflow

Before starting a new sprint:
- Read PROJECT_STATE.md
- Check DECISIONS.md
- Do not rely on previous chat history as source of truth

During implementation:
- Always specify full file paths
- Do not create new files without agreement
- Keep changes minimal
- Run ruff check and pytest before committing
