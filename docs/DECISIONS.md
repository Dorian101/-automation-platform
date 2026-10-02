## 2026-07-06

### Decision
Используем SQLite вместо PostgreSQL.

### Reason
Проект находится на стадии MVP.

### Revisit
После появления нескольких пользователей или необходимости удалённого размещения.

## 2026-07-07

### Decision
Plugin lifecycle moved into BasePlugin.

### Reason
Core must not know plugin implementation details.

### Result
Plugins became self-managed through on_startup() and on_shutdown().

## 2026-07-08

### Decision
Introduce shared Database helper for new repositories.

### Reason
New database components should not duplicate SQLite connection logic.

### Tradeoff
Existing repositories are not migrated yet to avoid unnecessary changes.

## 2026-07-08

### Decision
Use centralized error handling at Dispatcher level.

### Reason
Avoid duplicated exception handling inside plugins.

### Result
All plugin errors are logged consistently through the platform layer.

## 2026-07-14

### Decision
Migrate production storage from SQLite to PostgreSQL.

### Reason
SQLite was sufficient for MVP, but PostgreSQL is more suitable for persistent VPS deployment and future scaling.

### Result
Added PostgreSQL schema management through sql/schema.sql. Repositories migrated to psycopg-based access. VPS deployment now uses PostgreSQL as the primary storage.

## 2026-07-16

### Decision
Introduce SQL migration system for PostgreSQL schema management.

### Reason
Database schema changes must be versioned, reproducible and automatically applied during application startup. Maintaining a single schema.sql file does not scale as the project grows.

### Result
Implemented MigrationRunner with automatic migration discovery and execution. Added schema_migrations table for tracking applied migrations. Database structure is now stored in versioned SQL migration files.

### Tradeoff
Applied migration files become part of project history and must not be modified or removed after deployment.

## 2026-07-16

### Decision
Store database schema exclusively in migration files.

### Reason
A single source of truth is required for database structure. Keeping both schema.sql and migrations would lead to duplication and potential inconsistencies.

### Result
Created baseline migration 001_initial.sql and removed sql/schema.sql. New environments can now create the database schema entirely through migrations.

### Tradeoff
All future schema changes must be implemented through new migration files instead of direct schema editing.

## 2026-08-26

### Decision
Use context managers for all database connections.

### Reason
Repositories previously opened connections manually without guaranteed cleanup. Exceptions between connect() and close() caused connection leaks.

### Result
All repository methods now use `with database.connect() as conn` and `with conn.cursor() as cur`. Connections are automatically closed even on exceptions.

## 2026-08-26

### Decision
Pass shared Database instance to all plugins instead of creating individual instances.

### Reason
Each plugin was creating its own Database() instance, which duplicated connection configuration and made it impossible to share a connection pool in the future.

### Result
main.py creates a single Database instance and passes it to all plugin constructors. MigrationRunner also receives this shared instance.

### Tradeoff
Plugin constructors now accept a database parameter, which is a minor API change.

## 2026-08-26

### Decision
Wrap migrations in explicit transactions with rollback.

### Reason
If a migration SQL partially applied and the INSERT into schema_migrations failed, the migration would be applied but untracked, leaving the database in an inconsistent state.

### Result
apply_migration() now uses try/except with conn.rollback() on failure, ensuring atomicity.

## 2026-08-26

### Decision
Fix timezone handling for reminders to use UTC consistently.

### Reason
remind_at column is TIMESTAMP (without timezone), but datetime.now(UTC) returns timezone-aware datetime. On PostgreSQL servers with non-UTC timezone (e.g. Europe/Moscow), this caused future reminders to be treated as already due.

### Result
remind_at values are now stored as naive UTC strings. The get_due query uses NOW() AT TIME ZONE 'UTC' for consistent comparison.

## 2026-08-26

### Decision
Add database indexes on chat_id columns and reminders.is_sent.

### Reason
All repository queries filter by chat_id, and get_due() filters by is_sent. Without indexes, these are sequential scans that degrade as data grows.

### Result
Created migration 002_add_indexes.sql with indexes on notes.chat_id, reminders.chat_id, reminders.is_sent, clipboard.chat_id.

## 2026-08-26

### Decision
Namespace user identity by transport.

### Reason
`chat_id` is a Telegram concept. A web interface needs its own identity, and
reusing chat_id would make Telegram user 777 and web user 777 the same person.
Different transports are different identity spaces.

### Result
Introduced `Identity` (app/core/identity.py) stored as `kind:id`. Migration
003 changed `user_id` from BIGINT to TEXT in all three tables, prefixing
existing rows with `telegram:`. Repositories now accept `Identity` instead of
an integer chat id.

### Tradeoff
Group ids stay negative and remain valid as strings, but the column can no
longer do numeric range queries.

## 2026-08-26

### Decision
Keep plugin logic transport-independent via execute().

### Reason
Handlers written against aiogram's Message cannot be reused on the web, so
every feature would need two implementations.

### Result
`BasePlugin.execute(command, args, identity)` holds the logic and returns a
transport-agnostic `CommandResult`. `PluginManager` resolves commands from the
plugins' declared `commands` and dispatches. PluginManager.build_router()
produces the Telegram router, the web layer calls the same execute path.

### Tradeoff
Plugins can no longer answer inline or accept arbitrary Telegram-only input
through execute(). Plugins needing that override `router()`, which remains
available as an optional extension point.

## 2026-08-26

### Decision
Introduce a notification channel layer.

### Reason
Reminders must be delivered to whichever transport the user arrived on, but
the reminders worker previously held the bot directly, which is Telegram
specific and blocks any other transport.

### Result
`Notifier` routes to a `NotificationChannel` chosen by `identity.kind`.
TelegramChannel sends through the bot. WebChannel logs, since the web
interface has no inbox yet. RemindersPlugin now depends on Notifier.

### Tradeoff
Web reminders are stored and delivered through a logging stub. The worker
already marks a reminder sent after successful delivery, so a real inbox
means replacing WebChannel only.

## 2026-08-26

### Decision
Add a web interface as a second entry point, sharing the platform process.

### Reason
The user wants a browser alternative to Telegram. Running the web server in
the same process keeps one event loop, one plugin manager and one systemd
unit instead of duplicating bootstrap logic.

### Result
WebServer (app/web/) exposes GET /, GET /api/commands, POST /api/command and
GET /health, bound to WEB_HOST and WEB_PORT. main.py starts polling and the
web server together and stops both in one finally block.

### Tradeoff
Telegram and web availability are coupled: if the bot token is invalid the
web interface does not start, because the process exits early.

## 2026-08-26

### Decision
Defer user accounts, but add a shared-secret header check before the proxy.

### Reason
The web interface must open on a corporate laptop, and there are no other
users. Deferring accounts is reasonable; leaving the endpoint reachable by
anything that can open a socket is not. Identity resolution alone is not
access control.

### Result
Two layers in front of the app: a reverse proxy terminating TLS and
requiring credentials, and a `X-Platform-Auth` shared secret that only the
proxy can supply. The check lives in `verify_access()` and runs as middleware,
so it covers every route including `/health`. `hmac.compare_digest` avoids
timing leaks.

### Tradeoff
The secret must reach Caddy's config, and rotating it means editing two
places. Both layers are still necessary: the proxy protects the network edge,
the secret guarantees nothing bypassed the proxy.

## 2026-08-26

### Decision
Bind the web interface to loopback and use a reverse proxy.

### Reason
Exposing the port directly would mean either no authentication or inventing
authentication inside the app. The proxy keeps TLS, credential handling and
rate limiting outside the platform.

### Result
`WEB_HOST` defaults to `127.0.0.1`, so nothing but the local proxy can reach
the port. Only 443 is opened on the VPS. Documented in docs/DEPLOYMENT.md.

## 2026-08-26

### Decision
Add ruff as dev dependency and pytest test suite.

### Reason
No linting or testing infrastructure existed. Code quality issues (unused imports, formatting, type inconsistencies) went undetected.

### Result
Added ruff with E/F/I/W rules. Added pytest with 87 tests covering identity, command parsing, repositories, migrations, plugins, the manager, notification channels and the web layer. Tests run against a dedicated test database (automation_platform_test).
