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