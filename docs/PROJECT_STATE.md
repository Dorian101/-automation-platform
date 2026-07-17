# Project State

## Current version
v0.13.0

## Project goal
Automation Platform is a personal Telegram-based automation system built around an extensible plugin architecture.

The project goal is to provide a modular platform where new automation features can be added as independent plugins without changing the core application.

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
		MigrationRunner
   		    │
    		▼
  		PostgreSQL


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
* PostgreSQL repository

⸻

## Reminders

Purpose:

* scheduled notifications

Implemented:

* create reminders
* PostgreSQL storage
* background worker
* self-managed lifecycle

⸻

## Clipboard

Purpose:

* personal temporary text storage

Implemented:

* /copy <text>
* /paste
* PostgreSQL repository

⸻

## Database

Current database:

* PostgreSQL
* common database.py for all plugins
* SQL migrations support

Database schema:

sql/migrations/
└── 001_initial.sql

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
* Database schema changes are managed through SQL migrations.
* Migrations run automatically during application startup.
* Applied migrations are tracked in schema_migrations table.

app/db/database.py

⸻

## Current limitations

* Single-user oriented architecture.
* No authentication system.
* No external integrations.
* No web interface.

⸻

## Potential tasks:

* improve plugin error handling;
* add plugin startup/shutdown logging;
* add platform health checks;
* implement automated database backups;
* prepare foundation for future multi-user support.

⸻

## Development rules

* Keep core independent from plugin internals.
* New functionality should be implemented as plugins.
* Avoid unnecessary architectural complexity.
* Prefer incremental improvements over large rewrites.