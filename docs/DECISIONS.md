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

## 2026-10-06

### Decision
Use Caddy from the Ubuntu 24.04 universe repository, not the official
upstream repository.

### Reason
The deployment target is a single small VPS, and adding a third-party apt
source is another trust relationship and another maintenance surface for
one package. Ubuntu's version receives backported security fixes.

### Result
Caddy 2.6.2. The constraint this imposes: the directive is `basicauth`, not
`basic_auth`, which needs 2.8+. Anyone upgrading Caddy from upstream must
rename it, and anyone reading old notes must not assume the newer name
applies here.

## 2026-10-06

### Decision
Delegate both domains to Ruvds nameservers, and treat the Ruvds panel as the
only place DNS can be edited.

### Reason
DNS was initially edited at reg.ru for `.online` while its zone was already
served by Ruvds. Those edits were applied to a non-authoritative copy of the
zone and silently had no effect — the SOA serial never changed, which was the
only reliable signal that nothing was happening.

### Result
`ns1.ruvds.com` and `ns2.ruvds.com` for both `.ru` and `.online`. reg.ru's
DNS panel is no longer authoritative for either, and `ns1.reg.ru` now answers
REFUSED for `.ru`. Checking who actually serves a zone before trusting a
panel's display became a documented step in docs/DEPLOYMENT.md.

## 2026-10-06

### Decision
Let Caddy own TLS entirely; reject the free DomainSSL offered with the `.ru`
domain.

### Reason
Caddy obtains and renews Let's Encrypt certificates on its own. DomainSSL
requires manual installation and manual renewal once a year, and a
certificate Caddy did not obtain is one it will not renew — so it fails
silently when it expires. That is an extra failure mode with no benefit over
what already works.

### Result
No certificate file on disk to monitor or rotate. Side finding: the
`_globalsign-domain-verification` TXT record added for DomainSSL is not
present in the authoritative zone, so that certificate could not be issued
regardless. Recorded to stop the next person chasing it.

## 2026-10-06

### Decision
Load `.env` from inside `app/core/config.py`.

### Reason
`Config` reads `os.getenv` into class attributes at import time, but the
entry point called `load_dotenv()` after importing the app modules — too
late. On the server every database setting silently fell back to its default
and the service crash-looped with `fe_sendauth: no password supplied`, which
reads like a database problem rather than an import-order problem.

### Result
Config no longer depends on import order at the entry point. Covered by a
subprocess test in tests/test_config.py that fails if the fix is reverted,
because a module-level ordering bug is invisible to in-process tests.

## 2026-10-06

### Decision
Move authentication out of Caddy and into the application: drop `basicauth`
from the Caddyfile and introduce real accounts with a login page.

### Reason
`basicauth` gives one shared password to everyone. Every request behind it is
indistinguishable from every other, so there is no answer to "who wrote this
note" and no way to remove one person's access without changing the password
for everybody. The credential also lived as a hash in a config file that only
guarded the edge — the application itself trusted anything Caddy forwarded.

The two checks that existed had been conflated. A shared secret in a header
answers "did this come through the proxy", which is a transport property; a
login answers "who is asking", which is an account property. Collapsing them
into one credential is what made per-user data impossible.

### Result
Two layers with separate jobs. `X-Platform-Auth` still proves the request
came through Caddy and is still injected by Caddy, so the user never sees it
and it costs nothing in the interface. The session cookie identifies the user
and is what every plugin already keys its data on — `Identity(WEB, username)`
instead of the fixed `web:default`, which required no change in any plugin or
repository. Caddy is back to doing TLS and nothing else.

## 2026-10-06

### Decision
Hash passwords with `hashlib.scrypt` from the standard library, and store only
the SHA-256 of each session token.

### Reason
`passlib` or `bcrypt` would add a dependency for something Python already
ships. scrypt carries a per-password salt and, unlike plain iteration counts,
demands memory — which is the resource a GPU attack is short of. Parameters
are stored alongside each hash so the cost can be raised later without
invalidating existing accounts.

Storing the token itself would mean a dump of the `sessions` table hands over
live sessions. Storing its digest means the dump is useless, and deleting a
row is enough to log that session out.

### Result
No new dependencies. A leaked `users` table yields hashes that cost 37 ms each
to check; a leaked `sessions` table yields nothing usable. Both are covered in
tests/test_passwords.py and tests/test_accounts.py.

## 2026-10-06

### Decision
Set the test environment at module level in `tests/conftest.py`, above the
first import of the application.

### Reason
`Config` evaluates `os.getenv` once, when its class body runs. The env vars
were being set in a session-scoped fixture, which pytest runs *after*
collecting — that is, after every test module has already imported
`app.core.config`. The values were silently ignored and all 97 tests ran
against the development database, emptying it on every run while appearing
green.

The second half of the same bug: `_clean_db` dropped an explicit list of
tables, so `users` — created by the new migration and missing from that list
— survived, and the next migration failed on `relation "users" already
exists`.

### Result
`conftest.py` carries `# ruff: noqa: E402` deliberately; the imports below the
env block must stay below it. `_clean_db` now drops every table in the public
schema, so a future migration cannot reintroduce the stale-table failure.
Recorded because the suite stayed green throughout, which is exactly what
makes this worth writing down.


## 2026-10-06

### Decision
Do not add the second Caddy listener on `127.0.0.1:8081`.

### Reason
It was the missing half of the SSH-tunnel fallback for the corporate laptop.
A browser cannot send `X-Platform-Auth`, so a plain forward to port 8080 would
have got nothing but `401`; the fix was a second listener on loopback whose
entire job is to inject that header. It would have been extra production
surface serving no user-facing purpose, guarded only so that nobody reached
the app without going through the first listener.

The problem went away instead: the employer issued a corporate MTProto
gateway, and the bot reaches the platform through it with no tunnel at all.

### Result
Nothing to build and nothing extra to guard. `DEPLOYMENT.md` now records why
the tunnel is not the answer rather than leaving a task marked "not
implemented yet".

## 2026-10-06

### Decision
Run backups from a systemd timer calling `python -m app.manage backup`, built
on the `BackupManager` that had been sitting in the codebase uncalled.

### Reason
Two alternatives were on the table: a shell script, or letting the
application schedule its own backups.

A shell script would have been about five lines, but it would have been the
only executable logic in the repository with no test around it, and it would
have been typed onto the server by hand — which is where mistakes are made.
`BackupManager` already existed for pre-migration dumps and was flagged in
`PROJECT_STATE.md` as "exists but is not called"; making it general costs
little, and a `backup` subcommand on the existing `manage.py` costs less than
a new entry point.

Running it from systemd rather than from inside the application is the part
that actually matters: the scheduler has to work when the application cannot,
and a timer has no opinion about whether the platform starts.

### Result
`create_backup(name)` and `prune(retention_days)` replace the migration-only
method, so the same class can serve both a scheduled run and a pre-migration
one later. Dumps are written through a file descriptor opened at mode `0600`
rather than through `pg_dump -f`, which would have created them at the umask
default and only tightened the mode afterwards — the dump carries scrypt
password hashes. A retention of `0` means keep everything, not delete
everything.

12 tests in `tests/test_backup.py`. The restore path was exercised against a
scratch database: tables, ownership, recorded migrations and sequences all
came back. What is still *not* wired is the pre-migration call inside
`MigrationRunner`, and `PROJECT_STATE.md` says so.

## 2026-10-06

### Decision
Add a sign-up page gated by an invite code, replacing the host-only account
creation that had been deliberate until now.

### Reason
The old rule was that a public sign-up form on a private tool is an open
invitation, so accounts were made from the machine only. That was right about
the open form and wrong about the conclusion: the problem was never that a
registration page exists, it was that anybody reaching it could use it.

An invite code separates the two. It has to be typed to get in, it is never
stored, and rotating it does not disturb the accounts that already exist.
Leaving it unset reproduces the old behaviour exactly — no link on the login
page and a 404 on `/signup` — so a deployment that never configures it is no
more exposed than before.

What makes registration acceptable at all is how little a registered account
gets: an isolated namespace and the commands `/notes`, `/reminders`, `/copy`,
`/plugins`, `/help` and `/status`. Nothing executes anything, and nothing
reaches another user's rows.

### Result
`signup_allowed()` refuses an empty configured code even against an empty
submitted one, because `compare_digest` would happily match empty to empty —
that is the single line standing between "variable not set" and an open door.
There is no rate limiting anywhere (Caddy 2.6.2 cannot do it, and the app does
not), so the entropy of the code is the protection; `openssl rand -hex 16`.

The card styles moved into a shared `_styles.html`, because sign in and sign
up are the same layout and two copies would have drifted. 15 tests were added:
the gate itself, failed attempts leaving nothing behind, the
disabled-by-default behaviour, and a check that no template placeholder
survives rendering — a forgotten replacement only ever shows up in a browser.
