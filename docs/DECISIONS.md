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
Delegate every domain to its nameservers and treat the nameserver provider's
panel as the only place DNS can be edited.

### Reason
Records were first edited in the registrar's panel while the zone was already
served somewhere else. Those edits went to a non-authoritative copy and
silently had no effect — the SOA serial never changed, which was the only
reliable signal that nothing was happening. A registrar and a nameserver can
belong to different companies, and the panel you bought the name from is not
necessarily the one answering for it.

### Result
Both domains point at the nameservers actually serving them. Checking who
serves a zone — `dig NS`, then query those servers directly — before trusting
any panel's display became a documented step in docs/DEPLOYMENT.md.

## 2026-10-06

### Decision
Let Caddy own TLS entirely; reject the free certificate offered with the
domain.

### Reason
Caddy obtains and renews Let's Encrypt certificates on its own. A bundled
certificate requires manual installation and manual renewal once a year, and a
certificate Caddy did not obtain is one it will not renew — so it fails
silently when it expires. That is an extra failure mode with no benefit over
what already works.

### Result
No certificate file on disk to monitor or rotate. Side finding: the TXT
verification record added for the bundled certificate was never present in the
authoritative zone, so that certificate could not have been issued anyway.
Recorded so the next person does not go looking for it.

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

## 2026-10-09

### Decision
Record a link between a web account and a Telegram chat in its own table,
`telegram_links`, one-to-one in both directions. Do not move the data and do
not merge the two identities.

### Reason
The reminders plugin was written for Telegram: a reminder that arrives as a
sound on one of twenty open tabs is not a reminder. But the web interface is
where the same commands are easiest to type, so reminders set there have to
reach the chat — and that was impossible while `identity.kind` alone decided
delivery, since a `web:` identity lands in `WebChannel`, which logs and drops.

There were two ways to fix that. Merging the identities would have been
simpler — one namespace, no routing decision anywhere — and it was rejected:
`user_id` is a text column in `notes`, `reminders` and `clipboard`, so merging
means rewriting every row, and it would contradict the reason namespacing
exists, namely that one numeric id on two transports is not one person.

Keeping the identities apart and recording the pairing instead costs a lookup
and moves no rows. It also stays reversible: an unlink has nothing to undo,
which a namespace change would not have.

A separate table rather than a column on `users`, because that table is about
web credentials and a link is not an account — it is a statement that two
identities belong to the same person. `UNIQUE` on `telegram_id` enforces the
one-to-one: without it a shared login would silently merge two people's data,
which is the failure the table exists to prevent.

### Result
Storage only. `TelegramLinksRepository` and migration `005_telegram_links.sql`;
`python -m app.manage link-telegram|unlink-telegram|list-links` for the case
where the browser cannot reach Telegram. Re-linking replaces the previous chat
and says so, rather than overwriting it silently.

Nothing reads the link yet — delivery and the shared-data read are the next
two steps — and no authentication through Telegram exists yet, so the link
cannot yet be created from a browser. `Tests: 16 in tests/test_manage_links.py,
17 in tests/test_links.py`, including that linking moves no data.

## 2026-10-09

### Decision
Link accounts through Telegram's own Login Widget rather than a one-time code
typed into the console.

### Reason
Both approaches end with the same table, and both have the user confirm
ownership. The difference is what has to be built and what can go wrong.

A code means a table to store codes with expiry, a limit on guesses (the app
has no rate limiting anywhere, and a short code is exactly what that invites),
a handler in the bot, and a page that copies the code across. The widget means
verifying a signature, and Telegram's own button on a domain registered with
`/setdomain`.

The signature alone is not enough, and this is the part worth being explicit
about: a valid payload proves a real Telegram user signed it, not that the
person at the keyboard is that user. Someone who obtains their own valid
payload could otherwise ask any other browser to submit it, and that person's
account would be linked to the attacker's chat — handing over every reminder
sent to it. So the payload is bound to the browser that asked, with a nonce
issued in an `HttpOnly` cookie and spent on the first use, and `auth_date` is
checked so a captured signature cannot be replayed years later. Two checks
beyond the signature, and the widget was the cheaper place to put them.

### Result
`TELEGRAM_BOT_USERNAME` gates the whole flow: unset means no button and a 404
on the route, which is what a deployment that never registered a domain should
see. `Config.BOT_TOKEN` is read once, in config, rather than by a second
`os.getenv` in `main.py`.

Verification errors are deliberately vague — an unsigned payload, a wrong
signature and a wrong bot all produce one message. Naming the check that
failed tells an attacker which one to work on, and the detail belongs in the
log, which is what the `logger.warning` is for.

26 tests in tests/test_telegram_auth.py for the signature, 30 in
tests/test_telegram_link.py for the flow, including the nonce theft stated as
the attack it prevents. Linking still changes no delivery behaviour: a
reminder set on the web is still dropped by WebChannel until that is the next
step.

## 2026-10-09

### Decision
Route delivery through a resolver in `Notifier` rather than hardcoding Telegram
in the reminders plugin, and give up on a reminder that has nowhere to go.

### Reason
The reminder was written for Telegram. A reminder that arrives as a sound on
one of twenty open tabs is not a reminder, so the web interface had to be able
to reach the chat — and while `identity.kind` alone picked the channel, a
`web:` identity landed in `WebChannel`, which logs and drops.

Hardcoding "reminders go to Telegram" inside the plugin would have been fewer
lines, and it was rejected: it would put transport knowledge in the one place
that had none, and leave `WebChannel` registered but unreachable. A resolver
keeps the decision in one function that returns an identity — which may name a
different transport than the one the request arrived on, which is the whole
point of pairing the accounts.

Nothing is merged and no row is rewritten. Web data stays under `web:<name>`;
what moves is the delivery, so an unlink is still just a delete.

The second half is about a reminder with nowhere to go. It was retried every
thirty seconds for ever, because the loop could not tell "not delivered yet"
from "cannot be delivered at all" — one unlinked account would have filled the
journal indefinitely. `Notifier.deliver` now returns whether anything was sent,
and the worker gives up after three passes. A delivery that *raises* is still
retried for ever, because that one is a Telegram outage and waiting it out is
correct. Linking after two failures still works: the common sequence is setting
a reminder and linking a minute later, and that must not lose it.

### Result
`/status` reports where a reminder from this identity would actually arrive.
A reminder that goes nowhere is otherwise invisible: the command succeeds, the
plugin works, and nothing arrives.

14 tests in tests/test_delivery.py for the routing, 8 in tests/test_plugins.py
for the worker's decisions. The worker was split into `process_due()` so those
decisions can be exercised without a task that never returns.

309 tests pass. A reminder set on the web now reaches the linked chat, and an
unlinked one still reaches `WebChannel` exactly as before.

## 2026-10-09

### Decision
Widen reads across a pair and leave writes where they are.

### Reason
The two namespaces were kept apart so that one numeric id on two transports
could never be mistaken for one person. Pairing them says two identities are
one person, so a note written in the bot has to be readable in the console —
otherwise pairing only changes where reminders go, and half of its value is
missing.

Widening the read is cheap: `PersonResolver.identities()` returns the identity
plus whatever it is paired with, and the repositories take a list. Nothing is
rewritten, so a stored row still records who wrote it and from where, and the
history does not lie about its own provenance.

Widening the *write* would have been wrong in a way that is easy to miss. A
note added on the web would land under the Telegram identity, and unlinking
would then need to put it back — which means moving rows, in the direction that
cannot be verified. Keeping writes where they happen makes unlinking a single
delete, which is what made it safe to make links reversible in the first place.

The clipboard is the exception that proves the rule. It is one buffer shared
between two entry points rather than a history, so `/paste` reads across the
pair and the last write wins whoever made it. Copying a link on the web and
pasting it in the bot is the case the whole feature is for.

### Result
Reads span the pair in both directions, not just from the web: without the
reverse, a pair would be one-directional, which is not the same person. An
identity with no pair — every Telegram user, and any web account that never
linked — resolves to itself, so an unpaired deployment behaves exactly as
before.

`PersonResolver` answers the same question as `LinkedChatResolver` and shares
its dependencies, so the two cannot disagree about who a link belongs to. A
malformed chat id is treated as "no pair" rather than an error, since it
cannot match a stored one.

Writes are unchanged on purpose and are documented as such, so a later change
to the storage model does not quietly alter them. 30 tests in tests/test_person.py,
plus plugin-level tests that the resolver is actually wired through, which is
the part that would otherwise look built and behave unpaired.

339 tests pass.

## 2026-10-09

### Decision
Address a note by its position in the list, never by its row id.

### Reason
Notes had no way to be removed, so they accumulated and nothing said what to do
with them once old. Deletion needs an address, and there were three candidates:
the row id, the text, or a position.

The row id is a global sequence, so printing it tells one person how many notes
other people have written and hands them a number to try. It is an address that
works across accounts, which is the wrong property for the one operation that
must not reach them. The text is not unique and is not typeable reliably.

A position is relative to the list the person is looking at, so it cannot point
at anything they cannot see, and it renumbers itself after a delete — which is
what makes it safe to hold onto an old list. The cost is that position 1 means
"your newest" rather than a fixed thing, which is why the numbering is recomputed
on every read and the error says how many there are.

The repository's delete is scoped to the identities being read as well as to the
id, so widening a read across a linked pair does not leave a note visible in the
console that cannot be deleted from it. That is the same reasoning that made
reading span the pair, and skipping it would have produced a note on screen with
no way to get rid of it.

`entries()` returns `(id, text)` so the plugin can map a position to a row, but
the id is never rendered.

### Result
`/notes` lists numbered, `/del <n>` removes one, `/clear` removes the lot. All
three widen across a linked pair, so a note written in the bot is deletable from
the console and vice versa. Unlinking is still a single delete, because
deletion moved nothing either.

30 tests in tests/test_notes_deletion.py. Existing note tests were updated to
the `(id, text)` shape rather than replaced, so the isolation tests that guard
one account against another are still there.

369 tests pass.

## 2026-10-09

### Erratum: the Login Widget matches a `<script>` element, not a button

### Doing
The console rendered the link offer as a `<button data-telegram-login=...>`
next to the widget's `<script>` tag. Telegram's widget looks for
`script[data-telegram-login]` and replaces that single element with its own
button, so it never matched — the page showed a button that did nothing on
click, and the whole flow looked broken.

### Why it was missed
The markup looked right and the script loaded; only Telegram's own search
could disagree, and it did so silently. Nothing surfaced the mismatch until a
real browser click.

### Rule
Both `data-telegram-login` and `data-auth-url` live on the widget's `<script>`
element. In the console that is `_telegram_panel()` in `app/web/server.py`;
keep the attributes on the script tag, which the widget replaces, and never on
a neighbouring element. Fix: commit `01e3f2b`.

## 2026-10-10

### Decision
Harden the public edges of the web interface: rate limit the two public forms,
send security headers on every response, raise the scrypt cost, bound what a
username can be, and log failed logins.

### Reason
An OWASP pass found the doors that invite the cheap attacks. There was no rate
limiting anywhere — the 2026-10-09 note about the invite code said it then and
it is true of the login form too — so the username and password fields were an
unbounded guessing surface reached from the whole internet. Responses shipped
no CSP, frame or nosniff headers, so the console's HTML could be embedded or
misdeclared by a browser. And a username was accepted with any bytes in it,
which also means it could carry a newline into a log line and forge one.

### How it is done

- **Rate limiting.** An in-process sliding window (`app/web/ratelimit.py`)
  keys on `X-Forwarded-For`, which is trustworthy here because every request
  has already passed the transport check — nothing reaches a rated handler
  without the proxy's header. `/login` is also keyed per username, so one
  account's typos do not lock out a whole network, and a successful login
  clears its own key. Denied attempts are not recorded, keeping memory bounded
  by the limit. In-process is all the sharing there is: the deployment runs
  one web process, so two windows cannot disagree.
- **Security headers.** Attached in a `response_prepare` hook (not a
  middleware), so they reach responses that only an exception produced. CSP is
  `default-src 'self'` plus `https://telegram.org` for the Login Widget,
  `frame-ancestors 'none'`, `object-src 'none'`, `form-action 'self'`;
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`. HSTS is emitted only when
  `SESSION_COOKIE_SECURE` is on, i.e. where the site really is TLS.
- **scrypt.** `SCRYPT_N` rose from 2^14 to 2^17, OWASP's non-interactive
  recommendation; `maxmem` follows (128 MiB of working memory now, allowed up
  to 256 MiB). Parameters are recorded per hash, so hashes made at the old
  cost still verify — no password migration.
- **Username.** Restricted to `[A-Za-z0-9._-]`, max 32 characters, at the one
  place names enter the system (`UsersRepository.create`). The alphabet bounds
  what a caller-supplied name can do to a log line; failed logins are logged
  with `%r` on the username for the same reason.
- **Defaults.** `5` login attempts per 60 seconds, `10` sign-ups per hour per
  address; each overridable in `.env`, `0` disables.

### Result
13 new tests: the 429 after the allowance is spent and its reset on success,
the per-username keying, the per-address sign-up limit, the headers present on
HTML and on error responses, HSTS only on secure deployments, the OWASP cost
and old-hash verification, and the username rejections. 382 tests pass. A
fresh deploy also gets `SIGNUP_ATTEMPTS_PER_WINDOW`-style rows in the
documented `.env` template.

The rate-limit settings are new in `.env`, not mandatory: the defaults apply
when nothing is configured, so an existing deployment upgrades to limited
forms just by restarting on the new code.

## 2026-10-10

### Decision
Give the state-changing forms a CSRF token, write the account and linking
events to the log, and stop logging the body of a web notification.

### Reason
The first sprint closed the doors that invite the cheap attacks; this one
closes the one that turns a signed-in session into someone else's action. The
sign-in, sign-up, sign-out and unlink forms were all submittable from any page
that could reach the origin, and nothing recorded that a sign-in, a new
account, a sign-out or a link change had happened — a success and an attack
looked the same in the log. The web notification placeholder, meanwhile,
wrote the whole message into journald, which is not a place a reminder's text
should sit.

### How it is done

- **CSRF (`app/web/csrf.py`).** The two anonymous forms (sign in, sign up) use
  a double submit: the page issues a random token in an HttpOnly, SameSite=Lax
  `platform_csrf` cookie and echoes the same value into a hidden field, and a
  submission is accepted only when the two match. The session-backed forms
  (sign out, unlink) instead carry an HMAC of the session token, derived from a
  per-process secret, so no extra state is stored and a value captured from one
  session does not work in another. The anonymous cookie is deleted once a
  session exists, since from then on the session-derived token is what the
  forms use.
- **Order.** The login and sign-up CSRF check runs before the rate limit and
  before any scrypt work, so a forged submission is not a real attempt and does
  not spend the allowance. A rejected anonymous submission re-renders its form
  with a fresh token and 400; a rejected session-backed one answers 403 in
  plain text, because reloading takes the browser back to the console where a
  fresh token is already rendered.
- **The CSRF cookie is not Secure.** Its value is not a secret — the identical
  string is in the page's HTML — so it needs only to survive the same request
  that carries the session cookie. Marking it Secure would break the forms on
  a deployment the browser already refuses to send the session cookie to,
  without protecting anything the session cookie's own Secure flag does not.
- **Audit logging.** Sign-in, account creation, sign out, and Telegram linking
  and unlinking are logged with `logger.info`, the username with `%r`; the
  `manage` commands log `create-user`, `link-telegram` and `unlink-telegram`
  through their own logger. Failures and rejected tokens stay at `warning`.
- **Web notification text.** `WebChannel` logs the recipient and the message
  length and withholds the text.

### Result
8 new tests: the console hands its forms the session-derived token, a logout
without one is refused and leaves the account standing, a logged-out browser
cannot drive the link button, and the web notification text never reaches the
log. Existing tests were updated to fetch the form and echo its token, the way
a browser does. 390 tests pass.

No new `.env` setting: the CSRF secret is generated per process, so a restart
invalidates outstanding tokens and a fresh page load issues new ones. Nothing
about an existing deployment's configuration changes.

## 2026-10-10

### Decision
Add account administration to the host command line — disable/enable, reset a
password, revoke sessions — and deliberately leave rename out.

### Reason
A leaked credential needs a response that does not depend on the browser, and
until now the only way to disable an account was to edit SQL by hand. The four
operations the owner asked for were disable and enable, a password reset, and a
way to kill a session that has leaked; three of those were missing entirely.

Rename is the odd one. An account's data is keyed by `web:<username>` in
`notes`, `reminders` and `clipboard`, so a rename would silently orphan every
row unless the same transaction rewrote all three tables — and every future
plugin storing a `user_id` would have to be remembered too. That is a trap
worth not setting until identity is anchored to something other than the name,
so rename is deferred rather than half-done.

### How it is done

- **Disable and enable** flip `is_active` through
  `UsersRepository.set_active`. Nothing else needed to change: `authenticate`
  and `resolve_identity` already refuse an inactive account on every sign-in and
  every request, so a disable takes effect immediately. Disabling also revokes
  the account's sessions, so a disable is a complete stop rather than a state
  the browser can keep poking.
- **Reset password** reads the new password with `getpass`, never as an
  argument, exactly like `create-user`. `UsersRepository.set_password` reuses
  `hash_password`, so the same length rule applies, and the new hash is written
  before the sessions are revoked: a password worth changing is a password worth
  assuming is known.
- **Revoke sessions** is `SessionsRepository.delete_for_user`, a single delete
  by `user_id`. The table stores only token digests, so there is no per-token
  lookup to offer and ending them all is the only story simple enough to
  trust. The account and its data are untouched.
- **No data is deleted** by any of the three. A disabled account keeps its
  notes, reminders and clipboard until it is enabled again; the commands only
  change whether it can sign in and whether it has live sessions.
- **Logging.** All four commands write through the `manage` logger, next to the
  web interface's audit events.
- **No migration.** `is_active` already existed (migration `004`), so this is
  repository methods and command-line plumbing only.

### Result
25 new tests: 7 at the repository level (`set_active`, `set_password`,
`delete_for_user`) and 18 in `tests/test_manage_accounts.py` covering the four
commands, including that an unknown account is refused before a password is
read. 415 tests pass. No new `.env` setting and nothing to migrate.

## 2026-10-10

### Decision
Give a plugin a page of its own, and let it declare that it is web-only

### Reason
The first analytics plugin showed that a command is the wrong shape for a
spending breakdown. Everything arrived as a string on the way in and left as
one on the way out: recording a cost meant `/spend 320 еда`, another month
meant a different command, and a chart cannot be built from a sentence. That
is the terminal problem the whole thing was meant to leave behind, reached
from the other direction.

Two ways out. The first was to teach the core a vocabulary for forms and
charts: a small platform feature with a large surface, which every plugin
would depend on and every addition to which would be a decision about the
platform rather than about the plugin. The second is to let a plugin describe
its own page and have the web layer paint the description. The second was
chosen — it moves the same small vocabulary from the core to the one component
that has to know it anyway, and a plugin stops being showable only if the
platform grows a branch for it.

Web-only is declared rather than checked inside the plugin, for the reason
transport independence has always been the rule here: a plugin that inspected
`identity.kind` would have to know it had two transports, and that knowledge
is what the router exists to hold. The commands are still declared, so a chat
answers "web console only" — a refused command and a missing one are different
answers, and only the second is true.

### How it is done
- `BasePlugin.page` is a path or `None`, and the manager builds the route table
  from what plugins declared. Matched on the path, so an unclaimed path has no
  owner and there is nothing to leak.
- `page_view(identity)` returns a description; `page_action(identity, action,
  payload)` handles what the page did and returns the same description with
  the change applied. The payload is never trusted: every value arrives as a
  string and the plugin converts and validates.
- The description travels as JSON and the browser paints it, so the first load
  and every redraw go through one renderer. The server draws no chart and the
  plugin writes no markup. Everything reaching the page goes through
  `textContent`, so a category name is text and never becomes markup.
- `web_only` is refused by `PluginManager.execute`, before the plugin is
  reached.

### Result
The expenses plugin, with a bar chart, a figure table, per-category share and
a comparison against the previous month. Both views come from one aggregation
and cannot disagree.

Money is Decimal end to end against a NUMERIC column, rounded only for
display, so a monthly total still adds up to what was actually spent. The
chart is scaled against the largest category rather than the total: a month
with one dominant cost would otherwise draw every other bar as a sliver, and
the share stays on each bar as a figure so both readings remain available.

The change of month is an action like any other rather than a hidden
parameter, so the month is a part of the same request that draws it.

46 new tests, 484 in total: month arithmetic across a year boundary, a month
number outside the twelve not being given a name, the chart and the table
agreeing, a previous month reported only when there is one, the repository's
storage-language errors translated into something a person can act on, the
page refusing a guest, and two accounts isolated on one page.

## 2026-10-11

### Decision
Model job application stages as a state machine, and draw the funnel from
the stages reached rather than the stages occupied

### Reason
The tracker was going to be a table with a status column, and the status
column was going to be free text. That is a small decision with a large
consequence: a free status can be set to anything, including back to the
opening one, and every conversion percentage above it stops meaning anything.
It is also the part of the schema most likely to come up on an interview, so
it was worth getting right rather than leaving to a dropdown.

Each stage declares which stages may follow it. `offer` and `rejected` are
terminal — an offer has nowhere to go, and a rejection stays in the list
because its count is part of what the funnel reports. The buttons on each row
are drawn from the same declaration the server enforces, so a move that is
not offered is a move that is refused, and a stale page cannot talk the server
into skipping a stage.

The funnel's arithmetic was wrong on the first attempt and a test caught it
before anything was deployed. A status is the stage an application is in
*now*, so an application at `interview` has already been answered. Counting
occupied stages therefore reported 2 answered out of 2 still waiting — a
hundred percent, out of four applications where half had progressed. The
figure that was wanted is how many reached a stage, so each rung is the sum of
that stage and every stage after it. The denominator is everything ever sent,
rejections included: a rejection is an outcome of having applied, so it
belongs in what the shares are of, and excluding it would flatter every later
stage. It is drawn beside the funnel rather than in it — an exit, not a rung,
and a bar there would mean the widths no longer lined up.

Silence is counted only while an answer is outstanding. Days accumulated past
a reply would grow for ever on a row that can never change again and would
say nothing about what is waiting; age stays on every row, because that is a
fact about the application while silence is about waiting. Five days, chosen
as a way of separating "waiting" from "forgotten" rather than as a judgement
about anyone's prospects.

### How it is done
- `STAGES` in the plugin is the single declaration: label, and the stages it
  may move to. The row's buttons are drawn from it and `_set_stage` checks
  against it, so the two cannot disagree.
- The repository enforces nothing about transitions — that is the plugin's
  business, not storage's. The schema's CHECK is what keeps a stored stage
  real on any path that writes one, the same bargain as the positive amount
  on expenses.
- `counts_by_status` is the whole of the funnel's arithmetic. Shares and
  percentages are derived in the plugin, so the chart and the table cannot
  disagree.

### Result
The applications page at `/applications`: a funnel, a table with the moves
each row allows, silence flagged from five days, and a form that refuses a
date in the future — it would sit in the list counting days that have not
happened.

Reads widen across a linked pair like every other read. Writes do not.

40 new tests, 537 in total. Among them: every legal move and every refused
one, a full run through the pipeline, the funnel arithmetic including the bug
that found it, one account's applications invisible to another, and the
machine enforced over the wire rather than only by which buttons are drawn.


## 2026-10-11

### Decision
Open a public demonstration at `/demo`, as a guest namespace rather than an
account

### Reason
A reader arrives from a résumé link. They have no account and are not going to
make one to look at a project: the registration form at the front door is
where the interest goes to die, and the personal console has nothing in it to
show a stranger anyway.

Two ways out. The first was to seed the site with a pre-made account and hand
the credentials out — which means a public account with a public password,
data anyone can edit, and no way to tell a visitor from the owner. The second
is what was chosen: the demonstration is reachable without a session and writes
to a namespace of its own that is never an account.

The guest is `web:guest:<token>`, and the token is the random half of a
cookie. No row in `users`, no password, no session — so there is nothing to
authenticate, nothing to leak from `users`, and cleaning up is a delete by
prefix rather than a walk over accounts. A username is restricted to
`[A-Za-z0-9._-]`, which cannot contain a colon, so `guest:…` cannot collide
with an account by construction rather than by convention.

The three rules the module rests on, each because breaking it is a hole rather
than a crash:

- **A guest never resolves a session.** The handlers build the identity from
  the cookie and from nothing else. The demonstration is the only part of the
  site reachable without signing in, so it is the only part that must not be
  able to act as somebody. A test reads the module's source and fails if a
  call to identity resolution appears — crude, but it fails on the day someone
  adds one rather than after it ships.
- **A guest is capped.** The page is on the open internet and its namespace is
  the only one an anonymous visitor can write to. Counted against the
  database, not kept in the process, because a counter resets on every restart
  and that is exactly when a cap is still needed.
- **A guest never becomes an account.** Nothing to disable, nothing to rename,
  nothing to delete an account for.

Namespaces are filled on arrival rather than on a schedule: a guest with
nothing in it is seeded, and one whose rows were cleaned up is seeded again.
That is why the cleaner can run at any time without coordinating with anything
— a visitor never meets an empty demonstration, because arriving is what fills
it. The seed is the plugin's own `seed_demo`, because what would be sensible
to show is a fact about the plugin; the demonstration page inventing expenses
would make it know about both.

### How it is done
- `app/web/demo.py` holds the whole of it, including its own route
  registration, so the reachable-without-a-session surface is something a
  reader can check in one place rather than something to hunt for in the route
  table.
- The rendering moved to `app/web/pages.py` and is shared: the same function
  paints the account's page and the guest's. The demonstration is not a second
  front end, it is these plugins with a different identity in front of them.
- `is_public_demo_path` matches the prefix *and* the slash. A bare
  `startswith("/demo")` would also have opened `/demoted`.
- `python -m app.manage prune-guests`, run from a timer for the same reason
  backups are: the demonstration should not grow while the application is
  unable to start.
- `/about` links to it and says the data is invented and that registering is
  not needed. It is said on the page as well as on the landing: a
  demonstration that does not declare its numbers fictitious reads as a leak
  of somebody's real spending.

### Result
`/demo` lists whatever pages the plugins declare, so a third plugin appears
there without the landing page being edited. Each page is fully interactive —
the same forms, the same actions, the same refusals — against the guest's own
data.

23 tests: the prefix test refusing `/demoted`, two visitors not sharing, a
namespace surviving between requests, the banner, the caps, pruning leaving an
account untouched, and the structural check that the module never resolves a
session. 586 pass.

## 2026-10-19

### Decision
Read the visit log a page at a time, and cut the owner's own events out in the query

### Reason
The log is an event log that only grows, and the page read the first two
hundred rows of it and stopped. Worse, the two numbers on the page were about
different sets: the count was taken over the whole log, and the table was the
first two hundred rows, so the number a reader saw and the rows under it did
not agree even before a filter was applied.

The `hide=1` box made that worse rather than better, because it was applied in
Python to rows the page had already chosen. Hiding the administrator was
therefore a filter over the slice, not over the log: the total counted their
visits, the table did not show them, and a page that says one number above a
table of a different size is a small thing to wonder about every time.

The fix is not to filter earlier but to ask for the same thing in one place.
The exclusion is now a condition of the query, `count` takes it too, and the
two are cut by the same line. The total is the number of rows the log holds
for other readers, and it matches the table under it.

The exclusion is written `IS DISTINCT FROM` and not `!=`, and that is not
stylistic. A visit by a reader with no session has no identity at all, and
`NULL != 'web:somebody'` is not true in SQL — a `!=` would have silently
dropped every anonymous row from the count the moment the owner asked to be
hidden, which is the opposite of what the box is for.

### How it is done
- `VisitsRepository.recent` takes an `offset` and an `exclude`; `count` takes
  the same exclusion. Both build their condition through one helper, so
  reading and counting cannot drift apart.
- The page reads `page=2`, fifty rows to a page. A page number past the last
  is clamped to the last rather than answered with an empty table, so a stale
  link lands on real rows.
- The filter and the box travel with the pager, because a reader who narrowed
  the log and then turned the page expects to still be looking at the same
  narrowing.
- The count line says three things: how many events the log holds, how many of
  them the filter keeps, and which of those are on the screen. The filter's
  number only appears when it differs, because a page with no filter has one
  total and not two of the same.

### Result
782 tests. The paging ones count connections rather than seconds, because
seconds would be slow and flaky: five events opened twenty-one connections and
thirty-five opened eighty-one before the fix, and the numbers are the same now.

The same investigation found the cause of a four-second page, which was not
where it was looked for. The visitor's own events were excluded by asking who
the owner is — two queries, two connections — **once per row**, and there is
no connection pool, so a page of two hundred events opened four hundred
connections to PostgreSQL. The server was never slow: fifty measurements from
the host itself never passed 0.146 s, and the delay was identical with the
proxy off. The mistake worth recording is that all fifty of those probes were
`/about` and `/login`, and the slow page was never measured, so the page was
exonerated by a measurement that could not have found anything.

## 2026-10-19

### Decision
Keep the owner's own measuring out of the visit log with a secret, not with an address

### Reason
The log records pages looked at and nothing about who looked: no address is
stored, deliberately, because an address would tell the owner nothing they did
not already have. The consequence is that the owner's own checking sits in the
same table as a stranger's, and the only thing that could tell them apart was
an address compared on the way in and then thrown away.

An address is the wrong filter for most of the work done against the site.
It is written on one machine, so it stops matching the moment that machine's
address changes, and it says nothing about a request made from anywhere else.
Measuring the site from a laptop on a different network, or through a proxy,
arrives from an address that is on no list — and every such request is
recorded as somebody visiting. That is not a hypothetical cost: measuring the
site once put a hundred and thirty rows into the table in an afternoon.

The alternative of not measuring the public pages was rejected. Ordinary work
visits them, and asking a tool to keep away from the pages it was asked to
look at is not a rule that survives a week.

### How it is done
- `VISIT_PROBE_TOKEN` and the `X-Platform-Probe` header. A request carrying it
  leaves no row, from whatever address it came, and covers sign-ins as well as
  page views.
- Compared in the same place the address is compared — on the way in, and
  never stored, so nothing is added to any table.
- Empty trusts nothing. `compare_digest` calls two empty strings equal, so an
  unset token compared against a missing header would have trusted every
  request on the site. The empty check comes first for the same reason it does
  in the invite code.
- Caddy passes the header through without being configured to, which was
  checked by asking for a page with it and watching the log stay empty, not by
  reading the config.

### Result
It is a shared secret and not a credential, and the documentation says so
where somebody will read it before relying on it: anyone holding the value can
keep themselves out of the log, and a header is no secret against someone
determined to send one. It is not a way to refuse a visitor, and the log was
never an audit trail.

## 2026-10-19

### Decision
The demonstration's front door mints the same guest its pages do

### Reason
A visitor arriving at `/demo` was recorded with no name at all, and the guest
namespace began at the first click into a plugin page. The result on screen
was one person as two rows with nothing joining them — an unnamed arrival and
a named visit — which is the question the page is opened to answer.

The old behaviour was deliberate, and the reason was recorded next to the test
that held it: a namespace for somebody who never clicked would be a row per
passer-by for nothing. But the namespace was never the cost. A token is a
cookie; rows appear only when a page is opened, which is what `_view` does and
the landing does not call. Arriving cost a cookie and a log row and nothing
else, while what it saved was nothing at all.

The "came and went without clicking" signal is not lost by this. It becomes a
token appearing exactly once, on `/demo`, on its own — which says more than an
unnamed row did, because an unnamed row read the same way as an unnamed row
from anybody else.

### How it is done
- `landing` calls `read_guest` and `set_guest_cookie`, as the plugin page and
  the action route already did, and leaves the identity on the request.
- The test that asserted the opposite was rewritten rather than deleted, and
  its reasoning kept: the exclusion of a namespace per passer-by is still
  true, it simply was never what the landing did.

### Result
Six rows under one token across three pages and two browser sessions, against
twenty-seven tokens with one row each before. The reason for the many was
never a second bug: it was a fresh browser context each time, and a fresh
context is a fresh cookie, which is correct.

## 2026-10-19

### Decision
Leave HTTP/3 enabled, and record that the hypothesis behind disabling it was wrong

### Reason
A five-second delay on one page of the administration area was measured, and
the trail of suspects ran through the server, the proxy and the protocol. The
server was excluded by fifty measurements on the host itself. The proxy was
excluded by the delay being identical with it off. The protocol was not
excluded: the browser spoke h3 and a command line spoke h2, over the same
connection to the same server, with four hundred milliseconds against eight
seconds.

HTTP/3 was therefore disabled in Caddy. It changed nothing, and the change was
reverted rather than left behind on a theory that had been measured wrong. The
delay was in the application all along.

It is recorded because the reasoning was not wrong — UDP is genuinely worse
where it is throttled, and a Russian reader on a corporate network is exactly
the case where it would be — and because the measurement that settled it is
the one worth keeping: aiohttp's access log writes the time the request
started in brackets and the time the line was written in the prefix, and the
gap between them is the duration, on every request, for free.

## 2026-10-19

### Decision
Refuse a web reminder for an account that has nowhere to receive it, instead of
storing it and dropping it silently

### Reason
A reminder created on the web for an account with no Telegram link was written
and then lost at first delivery. The delivery layer reported it as delivered —
`WebChannel` logs and drops, and `Notifier` handed that back as success — so
the worker marked it sent and the person was never told. The "give up after
three passes" path that the docs promised existed in the worker but was never
reachable, because the web placeholder always reported true.

The fix stops the loss at the point it starts. A web account with no link has
nowhere for a reminder to go, so the command refuses at create time and says
why: reminders are delivered to Telegram, and this account is not linked. There
is no grace period to hold the reminder open for, because the person is told up
front and nothing about the reminder changes by waiting.

### How it is done
- `NotificationChannel.deliver` returns whether the message actually left the
  app: `TelegramChannel` returns true after a real send, `WebChannel` returns
  false.
- `Notifier.deliver` forwards the channel's answer rather than always true, and
  a new `Notifier.destination` exposes the resolver without sending, so a
  caller can ask where a message would go before writing it.
- The reminders plugin asks `Notifier.destination` before storing the row and
  refuses a web identity whose destination is not Telegram.

### Result
Linked accounts and Telegram users are unchanged: they still deliver to the
chat. A web account with no link is told to link first instead of losing the
reminder. The worker's "three passes, then give up" path stays for reminders
left behind by the old behaviour, and a delivery that raises is still retried
indefinitely — that is an outage.

784 tests pass.

## 2026-10-19

### Decision
Put a connection pool in front of PostgreSQL instead of opening a connection
per query

### Reason
`Database.connect` opened a fresh connection for every query. That is fine
while queries are counted, and it is what made the visit-log page cost four
seconds once: two hundred events, four hundred connections, none of them slow
on their own. The fix back then was to ask once rather than once per row, which
masked the underlying cost but left it for the next loop that forgot to.

The pool removes the cost rather than relying on each caller being careful. It
also caps connection count, which is the database's defence against a request
spike: no more than a fixed number of connections are ever open at once,
whatever the request rate.

### How it is done
- `Database` wraps `psycopg_pool.ConnectionPool`, opened lazily on the first
  `connect()` and returned to the server by `close()`, which `main.py` calls on
  shutdown.
- `connect()` still returns a context manager with the same `with database.connect()
  as conn` shape every repository already used, so no repository changed. The
  pool commits on a clean exit and rolls back on an exception.
- The sizes are configuration, not constants: `DB_POOL_MIN_SIZE` (0),
  `DB_POOL_MAX_SIZE` (20) and `DB_POOL_TIMEOUT_SECONDS` (5), so a request that
  cannot borrow a connection fails fast instead of queueing on a full pool.

### Result
The test suite reuses one pool for the whole run rather than creating hundreds
of short-lived ones, which is what the full run exposed: a pool carries
background threads, so a fresh `Database` per test was a cost with no benefit.
784 tests pass. The "no connection pool" limitation is gone from the project
state.

## 2026-10-20

### Decision
List and cancel reminders by position, the way notes are addressed

### Reason
A reminder, once set, could not be seen or removed: there was nothing to
answer "what is coming up" or "that one is stale, take it back". The notes
plugin had already settled the addressing question — a note is deleted by its
position in the numbered list, never by row id — and a reminder wants the same
answer for the same reason: the id is a global sequence, so printing it leaks
how many reminders other people have and hands out a number to guess.

### How it is done
- `RemindersRepository.entries` returns the unsent reminders soonest first, and
  `delete` removes one scoped to the account and to the unsent state.
- The plugin answers `/reminders` with a numbered list and `/cancel <n>` with a
  removal by that position, refusing a number past the end or a non-number.
- The timestamp is shown in the reader's local time with the offset named, so
  "soon" can be compared against the clock on the wall.

### Result
A reminder is now cancellable the moment it is set, and what is coming is
visible without waiting for a delivery. 796 tests pass.

## 2026-10-20

### Decision
Turn the console's command list into clickable chips instead of plain text

### Reason
The console listed every command as `<code>` text with its description, so a
reader still had to type a command out by hand. The point of a web console is
to make the same commands the bot takes easier to reach, and a list you cannot
act on only shows what there is to remember.

The decision pulled against a bigger option and picked the small one. A command
signature — name, parameters, types — would let the page build real forms and
validate before calling, but that is a change to the plugin contract and to
`/api/commands`, and nothing in this step needed it. A chip that puts the
command into the input box gets the keyboard out of the way for the same cost
as the description already on the page.

### How it is done
- Each command in the list renders as a `<button class="chip">` rather than
  `<code>`. A click sets the input to `command + " "` and focuses it, so a
  command with arguments is one click plus the text, and a command without
  arguments runs as-is — the trailing space is harmless because the command is
  parsed after trimming.
- The chips are built from the same `/api/commands` payload the console already
  fetched, so no server change and no new contract. The button uses
  `textContent`, so a command name is text and never markup.

### Result
The command list is clickable on every transport that renders it, and nothing
below the surface moved. 796 tests pass.

## 2026-10-20

### Decision
Ship a share preview image, and give the site one mark — a hub, not a chat
bubble

### Reason
A résumé link is passed on in chat, and every share preview arrived as a bare
title because there was no `og:image`. Meanwhile the favicon was three stacked
bars, which reads as a messenger logo — message bubbles or a burger menu —
and the platform is neither. A preview image is the first binary asset the
repository would carry, which is the reason it had been deferred; it stops
being a reason once the image is reproducible from text.

Both problems are one problem: the site had no mark of its own. The platform's
shape is a hub that plugins plug into — `PluginManager` in the middle, the
six plugins around it, reachable from two transports — so the mark is that: a
core with modules radiating off it, joined by lines. Structure, not a
conversation.

### How it is done
- `app/web/favicon.svg` is redrawn as the new mark: a blue core, four corner
  modules, four spokes. It replaces the three bars.
- The preview is drawn as `app/web/og-image.svg` and rasterised to
  `app/web/og-image.png` at 1200×630. The PNG is what the `og:image` tag
  points at, because the parsers that render previews do not render SVG; the
  SVG stays beside it so the look is changed and re-rendered, never edited as
  a binary.
- `_share_tags` in `app/web/server.py` now emits `og:image`,
  `og:image:width` and `og:image:height` when `PUBLIC_BASE_URL` is set, and
  the Twitter card widened from `summary` to `summary_large_image`. A new
  `/og-image.png` route serves the file without a session, the same way the
  favicon does.

### Result
A shared link now carries a picture, and the tab icon and the preview show one
mark instead of two. 799 tests pass.

## 2026-10-20

### Decision
Let the share preview image through robots.txt

### Reason
The preview showed the title and description but no picture. The page the
messenger reads — `/about` — is allowed, so the text rendered from the first
request. The picture is a second request, for `/og-image.png`, and `Disallow: /`
refused it: a bot that respects robots.txt reads the page and then refuses the
image the page names. The fix is the same one the sitemap already needed, and
for the same reason — a file that is not page content still has to be
fetchable, or an entry point that depends on it fails in the least diagnosable
way: a 200 on one request and silence on the next.

### How it is done
- `_robots` in `app/web/server.py` adds `Allow: /og-image.png` beside
  `Allow: /sitemap.xml`, with a comment naming why the image is not page
  content to index, only something to fetch.

### Result
A shared link can now carry its picture end to end. 800 tests pass.

## 2026-10-20

### Decision
Run an argument-less command from its chip, and name which commands take one

### Reason
Every chip did the same thing: put the command in the input box. That leaves a
reader typing Enter after clicking `/notes`, `/help` or `/status`, when there is
nothing left to type. The console needs to know the difference between a
command that is complete on its own and one that needs the reader's input, and
the difference has to ride the metadata rather than a hardcoded list in the
page — a page that knows which commands take arguments is a page that forgets
one the day a plugin adds a command.

A plugin already declares `commands` (command → description). The new
`command_args` sits beside it and carries the argument placeholder for the
commands that need one; a command absent from it takes no argument.

### How it is done
- `BasePlugin` gains `command_args: dict[str, str]`, defaulting to `{}`. The
  plugins declare only the commands that take an argument: notes, reminders and
  clipboard fill it; system needs none.
- `/api/commands` includes `command_args`, so the console reads the rule rather
  than knowing it.
- The console's chip handler runs a command with no argument on the click, and
  puts one with an argument into the input box as before. The shared
  `execute()` from the form is reused, and focus is no longer forced back to
  the box after a chip-run command.

### Result
`/notes`, `/reminders`, `/paste`, `/plugins`, `/help` and `/status` run in one
click; the argument-taking commands still land in the box to be completed.
801 tests pass.

## 2026-10-20

### Decision
Give a command with arguments a labelled form instead of a bare text box

### Reason
Sprint one made the chips know the difference between a complete command and
one that needs input. This one acts on it for the commands that do: putting
`/add ` into the box and asking the reader to type the rest still leaves the
reader typing, and a command like `/remind` needs two things — minutes and
text — that a single box cannot hint at. Fields with labels do what a chat
line cannot: name each argument and stop the reader guessing its order.

The metadata already carried the seed, and this is the point where it grows
up. `command_args` was a placeholder string; it becomes a list of
`(type, label)` fields, so the page can draw the right input for each and a
prompt under it. The values are joined by spaces back into the `args` string
the command already parses, so the server and the plugins stayed untouched —
only the shape the page receives changed.

### How it is done
- `BasePlugin.command_args` becomes `dict[str, list[tuple[str, str]]]`: each
  field is a `(type, label)` pair, `type` being `text` or `number`.
- Notes declares `/add` as one `text` field and `/del` as one `number`;
  reminders declares `/remind` as `number` minutes then `text`; clipboard
  declares `/copy` as `text`. System still needs none.
- `/api/commands` serialises the fields to `[{type, label}]` per command.
- The console renders a command with arguments as a small form under its chip:
  one labelled input per field plus a "Выполнить" button. Clicking runs the
  command with the space-joined values; clicking an argument-less command
  still runs it at once. Focus returns to the main input after a run, the same
  as a manual submit.

### Result
A command with arguments is answered by filling in labelled fields, and a
command without them by one click. 802 tests pass.

## 2026-10-20

### Decision
Render reminder times in `DISPLAY_TIMEZONE`, and make running a command from
the list give back an answer in place

### Reason
Three rough edges from the console work:

- A chip or form runs a command, but the result is drawn in the panel above
  the command list, so a reader who just clicked has to scroll back up to find
  out what happened — and often does not, which reads as "nothing happened".
- Opening a form for one command left the forms of others open, so the list
  accumulated half-filled inputs that meant nothing.
- `/reminders` showed the due time in the host's own zone — UTC on this box —
  while the reader's wall is Moscow. The administration pages already carried
  `DISPLAY_TIMEZONE` for exactly this; reminders had simply stopped short of
  using it and converted to the process zone instead.

### How it is done
- `_format_remind_at` converts the stored naive UTC timestamp into
  `ZoneInfo(Config.DISPLAY_TIMEZONE)` rather than `astimezone()` with no
  argument, matching how `/admin/visits` already renders times.
- The console's `execute()` takes a `reveal` flag: a command run from a chip
  or form scrolls the result panel into view and briefly highlights it, while
  a manually submitted command does not — the reader is already looking at the
  box. A manual submit is the one that clears the input.
- Opening an argument form removes every other open form first, so at most one
  is ever showing.

### Result
A click out of the command list now answers "done" without a trip up the page,
and the times it reports are on the reader's own clock. 803 tests pass.

## 2026-10-20

### Decision
Give the administrative area a status page that answers "is it healthy" at a
glance

### Reason
`/admin` opened on the user list because that was the only thing in it, and the
only way to know the platform was alive was the health endpoint plus a look at
the host by hand. The deploy already restarts the service and tails the journal,
but the person running it had no single place that says disk, memory, load and
the newest backup without a trip to the shell. A dashboard is the page the area
was missing: it reads across every plugin's tables, which is the administrative
area's whole job.

The system figures are read from `/proc` and `os.statvfs` rather than `psutil`.
The service already runs under a systemd sandbox that keeps `/proc` readable,
`statvfs` is a read-only call the strict mount does not block, and a dependency
for three numbers is not needed.

### How it is done
- `app/web/system.py` reads disk, memory and load from the standard library only,
  returning `None` rather than zero when a figure cannot be read — a dashboard
  printing `0 B` on a host that keeps its memory elsewhere is lying about an idle
  box, and the page renders `None` as `—`.
- `/admin/status` renders a frame once; `/admin/status/data` returns one JSON
  snapshot the page refreshes every thirty seconds, so watching the disk does not
  mean reloading. The database counts are one `SELECT count(*)` per table rather
  than a join, so one slow table does not take the rest down with it.
- `/admin` now redirects to `/admin/status`, the overview, rather than to the
  user list.

### Result
The administrative area opens on a page that says whether the platform is
healthy without a click, and every figure is honest about what it could not
read. 808 tests pass.

## 2026-10-21

### Decision
Make the status page's labels read plainly, and drop the disk row that
duplicated the root volume

### Reason
Three things about the first cut read wrong:

- The disk card showed the backups' free space next to the root's, but both
  live on `/dev/sda1` — the same volume — so the two numbers were the same and
  the extra row only suggested there was a second disk. The backups' own size
  is already the backups card's job.
- `Load 1м` is jargon, and `Доступно`/`Используется` are less direct than
  `Свободно`/`Занято`. A status page is read for its numbers, and its labels
  should not need translating twice.
- `Последний визит` was formatted with `isoformat`, which carried a `+03:00`
  offset that lengthened the line until it overflowed the card. The visits and
  backups already rendered wall-clock times without an offset; this one had
  simply not been brought into line.

### How it is done
- The disk card is two rows — `Свободно` and `Занято` — and `system.disk`
  now reports the root volume only; the `backups` volume key and its helper
  are gone, since nothing reads them.
- Labels across the cards are plain words: `Средняя за минуту`, `Всего
  файлов`, `Создан`, `Занимают`.
- `_last_visit` formats `%d.%m.%Y %H:%M` in `DISPLAY_TIMEZONE`, matching the
  backups' timestamp, and the template stops doing its own `replace('T', ' ')`.
- The timestamp and the backup file name are allowed to wrap (`overflow-wrap:
  anywhere`, `min-width: 0`) so a long value breaks instead of spilling past
  the card, while the byte counts stay on one line.

### Result
The status page reads in plain terms, the disk card no longer shows a second
volume that does not exist, and nothing overflows the card. 810 tests pass.

## 2026-10-21

### Decision
Record a visit's referring origin, never its full URL

### Reason
The log answered when a visit happened but not where it came from: a stranger
arriving from a link on another site or from a search engine left the same
row as somebody who typed the address in. The question "which site led here"
is the one an owner wants answered, and it is what the `Referer` header is
for.

The full header is more than the answer. A search engine sends its result
path and the query typed into it, and a log of visits is not a log of
searches. Keeping only the origin — `https://example.com` — answers the
question without collecting it, which is the same reason the table stores no
address. Modern browsers already shorten a cross-site referrer to its origin,
so the shortening is also the honest description of what usually arrives.

### How it is done
- Migration `010_visits_referrer.sql` adds a nullable `referrer` column.
- `VisitsRepository.record` writes it and `recent` reads it, as a field on
  `Visit` defaulting to None.
- `_referrer_origin` in `server.py` reads the `Referer` header and keeps only
  scheme + host, rejecting anything unparseable or not http(s); the visit
  middleware passes its result through to `record`.
- `/admin/visits` gains an «Откуда» column, escaped like the path because the
  value came from a header and is data, not markup.

### Result
The log now says which site led a visitor here, up to its domain, and a direct
visit still reads as no referrer. 813 tests pass.

## 2026-10-23

### Decision
Give tasks their own page, drawn as a kanban board rather than a list

### Reason
A to-do pile is where things go to be remembered, so it grows. The tools that
store one only as a long vertical list make a pile that is painful to read past
a few dozen entries, and that is exactly the point a pile is worth having.

The answer is a board, not a table: three columns — todo, doing, done — and a
card that moves between them. The status is the one thing a board's columns
can express without inventing a filter UI, and it is the one attribute that
says what a person does with a pile: they pick from it, work on it, finish it.

The state machine is small but kept explicit, the same bargain as the
applications funnel: todo → doing → done, with the reverse of each move so a
mistake is not a one-way door, and a jump over a status is refused rather than
silently accepted. A card opens on click and every attribute edits in place,
so an empty priority or deadline reads as empty and is one click from being
filled.

### How it is done
- Migration `011_tasks.sql` adds `tasks` with text, status, priority, due_at,
  created_at (database-set) and a `notified_at` so a deadline is reminded once.
- `TasksRepository` is identity-scoped for writes and widened for reads, the
  same rule notes and the other personal plugins follow.
- `TodoPlugin` is web-only with a page, renders the board from the plugin's
  own description, and runs an hourly worker that reminds about deadlines
  through the notification layer — once per deadline, to the linked Telegram
  chat; an unpaired account is simply not reminded.
- The board and its inline editing are a new painter in `_plugin_page.html`;
  the plugin supplies only the description, so the platform knows nothing
  about tasks.

### Result
A page at `/todo` where a growing pile stays a board, moves through three
columns, sorts by priority, deadline or creation, and nudges about deadlines.
832 tests pass.
