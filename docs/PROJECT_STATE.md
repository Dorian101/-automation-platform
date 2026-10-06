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
* every route except /login and /health requires a session
* WEB_HOST binds to loopback, port 8080 never exposed

Accounts are created only from the host with `python -m app.manage
create-user`. Passwords are stored as salted scrypt hashes; only the SHA-256
of a session token is kept. Two web users cannot read each other's data.

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
└── 003_user_identity.sql

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

* Web accounts are managed from the host only: there is no registration, no
  password reset, and no way to disable an account other than by hand in SQL.
* Web notifications are dropped rather than delivered.
* No external integrations.
* Web console is functional but minimal.

⸻

## Potential tasks:

* give the web interface a real inbox so WebChannel can deliver;
* add reminder to /cancel and /reminders listing;
* call BackupManager before applying a migration, so a failed deploy has a
  dump from minutes ago rather than from last night;
* manage accounts beyond create and list — disable, rename, reset a password.

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
