# Project State

## Current version
v0.9.0

## Project goal
Automation Platform is a personal Telegram-based automation system built around an extensible plugin architecture.

The project goal is to provide a modular platform where new automation features can be added as independent plugins without changing the core application.

## Current status
The platform is operational.

## Implemented:

* Telegram bot integration
* Plugin architecture
* Persistent storage
* Background tasks inside plugins
* Several working automation plugins

## Current architecture

Layers

Telegram
    │
    ▼
Core application
    │
    ▼
PluginManager
    │
    ├── Plugin lifecycle
    ├── Router registration
    └── Plugin management
            │
            ▼
        Plugins
            │
            ▼
       Repository layer
            │
            ▼
          SQLite


## Core components

Core

## Responsibilities:

* application startup
* configuration loading
* logging
* Telegram bot initialization


## Plugin system

## Implemented:

* BasePlugin contract
* PluginManager
* plugin registration
* plugin metadata
* command metadata
* plugin lifecycle

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

⸻

## Notes

Purpose:

* personal notes storage

Implemented:

* create notes
* persistent storage
* SQLite repository

⸻

## Reminders

Purpose:

* scheduled notifications

Implemented:

* create reminders
* SQLite storage
* background worker
* self-managed lifecycle

⸻

## Clipboard

Purpose:

* personal temporary text storage

Implemented:

* /copy <text>
* /paste
* SQLite repository

⸻

## Database

Current database:

* SQLite

Pattern:

Plugin
  │
  ▼
Repository
  │
  ▼
SQLite

## Current state:

* Existing repositories may use their own SQLite connection.
* New repositories should use the shared database helper:

app/db/database.py

⸻

## Current limitations

* Single-user oriented architecture.
* No authentication system.
* No external integrations.
* No web interface.
* Existing repositories are not migrated to the shared database layer.

⸻

# Next sprint

v0.10.0

## Goal:

Improve platform stability before adding new functionality.

## Potential tasks:

* improve plugin error handling;
* add plugin startup/shutdown logging;
* add platform health checks;
* review database layer;
* prepare foundation for future multi-user support.

⸻

## Development rules

* Keep core independent from plugin internals.
* New functionality should be implemented as plugins.
* Avoid unnecessary architectural complexity.
* Prefer incremental improvements over large rewrites.