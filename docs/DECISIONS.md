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