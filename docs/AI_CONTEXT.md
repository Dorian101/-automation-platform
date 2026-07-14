# AI_CONTEXT

## Project goal

Automation Platform is a Telegram-based modular automation system.
Each feature is implemented as an independent plugin.

## Architecture principles

- Plugin-based architecture
- Repository pattern
- PostgreSQL persistence
- Separation of concerns
- Simple before scalable
- Database schema managed through sql/schema.sql

## Database layer

The project uses PostgreSQL as the primary storage.
Database access is centralized through the Database helper.
Repositories:
- notes_repo.py
- reminders_repo.py
- clipboard_repo.py
Database schema is stored in:
- sql/schema.sql

## Current plugins

### Notes
Stores personal notes.

### Reminders
Stores reminders and delivers them using a background worker.

### System
Provides service commands:
- /plugins
- /help

## Plugin contract

Each plugin exposes:

- name
- version
- description
- commands
- router()

Plugins are registered only through PluginManager.

## Plugin lifecycle

The platform owns the plugin lifecycle.

Plugins manage their own resources through:

- on_startup()
- on_shutdown()

Core application never knows plugin internals.

## Development rules

- Complete one sprint at a time.
- Update PROJECT_STATE.md after every completed sprint.
- Update AI_CONTEXT.md after every completed sprint.
- Prefer small, incremental refactoring.
- Avoid unnecessary complexity.
- Error handling is centralized at Dispatcher level.

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