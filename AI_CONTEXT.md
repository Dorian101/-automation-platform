# AI_CONTEXT

## Project goal

Automation Platform is a Telegram-based modular automation system.
Each feature is implemented as an independent plugin.

## Architecture principles

- Plugin-based architecture
- Repository pattern
- SQLite persistence
- Separation of concerns
- Simple before scalable

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

## Development workflow

Before starting a new sprint:
- Read PROJECT_STATE.md
- Check DECISIONS.md
- Do not rely on previous chat history as source of truth

During implementation:
- Always specify full file paths
- Do not create new files without agreement
- Keep changes minimal