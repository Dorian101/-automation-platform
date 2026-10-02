# AI_CONTEXT

## Project goal

Automation Platform is a modular automation system with two entry points:
Telegram and a web interface. Each feature is implemented as an independent
plugin that works identically on both.

## Architecture principles

- Plugin-based architecture
- Repository pattern
- PostgreSQL persistence
- Separation of concerns
- Transport-independent business logic
- Database schema managed through sql/migrations/

## Entry points

The platform exposes the same command set over two transports.

- Telegram through aiogram polling
- Web through aiohttp

Neither transport knows about the other. Both build an `Identity` for the
caller and hand it to `PluginManager.execute()`.

## Identity

Users are identified by `Identity`, stored as `kind:id` in the database
(for example `telegram:-1001234567890`, `web:default`).

Namespacing by transport means the same numeric id arriving on two transports
never resolves to the same user.

Web identity resolution lives in `app/web/auth.py`. It currently returns a
single fixed identity, and is the seam for user accounts.

## Access control

The web interface has no user accounts. It is guarded by two independent
layers, both of which are required:

1. A reverse proxy that terminates TLS and requires credentials.
2. A `X-Platform-Auth` shared secret in `WEB_ACCESS_TOKEN`, verified by
   `verify_access()` as middleware covering every route.

The secret guarantees nothing reached the app except through the proxy. The
proxy alone would not, if the port were ever exposed directly.

`WEB_HOST` defaults to loopback for this reason. See docs/DEPLOYMENT.md.

## Command flow

1. Input arrives on a transport
2. `parse_command()` splits it into command and arguments
3. `PluginManager.execute()` finds the owning plugin and calls `execute()`
4. The plugin returns a `CommandResult` carrying transport-agnostic text
5. The transport renders the result

Plugins declare their commands in `commands`. The platform routes them
automatically, so plugins contain no adapter code. A plugin overrides
`router()` only when it needs transport-specific handlers, such as inline
keyboards.

## Database layer

PostgreSQL. All access goes through the shared `Database` helper and
repositories using context managers.

Repositories:
- notes_repo.py
- reminders_repo.py
- clipboard_repo.py

Schema is versioned in sql/migrations/.

## Current plugins

- Notes: /add, /notes
- Reminders: /remind, delivers through notification channels
- Clipboard: /copy, /paste
- System: /plugins, /help, /status

## Plugin contract

Each plugin exposes:

- name
- version
- description
- commands
- execute(command, args, identity)
- router() (optional)
- on_startup() / on_shutdown()

## Notification layer

`Notifier` routes a message to the channel matching the target's transport.

- TelegramChannel sends through the bot
- WebChannel is a placeholder that logs, pending a web inbox

Adding a transport means adding a channel, nothing else.

## Web interface

- GET / — HTML console
- GET /api/commands — plugin and command metadata
- POST /api/command — execute a command
- GET /health — database health check

Bound to `WEB_HOST` and `WEB_PORT`, defaulting to 127.0.0.1:8080.

## Development rules

- Complete one sprint at a time.
- Update PROJECT_STATE.md after every completed sprint.
- Update AI_CONTEXT.md after every completed sprint.
- Prefer small, incremental refactoring.
- Avoid unnecessary complexity.
- Error handling is centralized at Dispatcher level.
- Business logic stays transport-independent.
- Run `ruff check app/ tests/` and `pytest` before committing.

## Development workflow

Before starting a new sprint:
- Read PROJECT_STATE.md
- Check DECISIONS.md
- Do not rely on previous chat history as source of truth

During implementation:
- Always specify full file paths
- Do not create new files without agreement
- Keep changes minimal

## Runtime

Application is launched in linux daemon with:
uv run python -m app.main

Production deployment uses systemd service on VPS.

## Tests

Run against a dedicated `automation_platform_test` database, dropped and
recreated per test. Covers identity, command parsing, repositories,
migrations, plugins, the manager, notification channels, and the web layer.
