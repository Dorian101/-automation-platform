# Project State

## Current version
v0.16.0

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

Plugin → Notifier → channel by identity kind
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
* no deletion — notes only ever accumulate

⸻

Reminders

Purpose:

* scheduled notifications

Implemented:

* create reminders
* PostgreSQL storage
* background worker
* self-managed lifecycle

⸻

Clipboard

Purpose:

* personal temporary text storage

Implemented:

* /copy <text>
* /paste
* PostgreSQL repository

⸻

## Web interface

Purpose:

* browser access to the same command set

Implemented:

* GET /login, POST /login — sign in form
* POST /logout — ends the session
* GET / — HTML console
* GET /api/commands — plugin and command metadata
* POST /api/command — command execution
* GET /health — database health check
* WEB_HOST / WEB_PORT configuration

Access control:

* reverse proxy terminates TLS and injects the X-Platform-Auth secret
* X-Platform-Auth shared secret, verified as middleware covering every route
* session cookie (HttpOnly, SameSite=Lax, Secure) issued at /login
* every route except /login, /signup and /health requires a session
* WEB_HOST binds to loopback, port 8080 never exposed

An account can be paired with a Telegram chat, stored in `telegram_links` and
one-to-one in both directions. The pair never moves data: web rows stay under
`web:<name>`, Telegram rows stay under `telegram:<id>`, and neither identity is
merged into the other. No delivery path reads the link yet.

Managed from the host with `python -m app.manage link-telegram <username>
<chat_id>`, `unlink-telegram <username>` and `list-links`, which is also the
way out when the browser cannot reach Telegram. There is no browser flow for
it yet.

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
* Notifier — routes by identity kind

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
* Web notifications are dropped rather than delivered. A linked account does
  not change this yet: the link exists but nothing consults it.
* No external integrations.
* Web console is functional but minimal.
* Notes can be added and listed but not deleted, so they accumulate with no
  way to clear one out or all of them.

⸻

## Potential tasks:

* read the linked chat when delivering, so a reminder set on the web reaches
  the bot instead of vanishing into WebChannel;
* add the Telegram link to the console: a button and an unlink, which means
  giving `/` its first page that renders account state rather than the result
  of a command;
* let a linked account read one person's notes and clipboard from both
  transports, which means reading by both identities at once — and deciding
  what "the last copied text" means across the two;
* let notes be deleted, individually and in bulk;
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
