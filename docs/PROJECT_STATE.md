# Project State

## Current version
v0.29.0

## Project goal
Automation Platform is a personal automation system built around an extensible plugin architecture, reachable over Telegram and a web interface.

The project goal is to provide a modular platform where new automation features can be added as independent plugins without changing the core application, and work identically on every transport.

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
- database indexes on user_id columns
- context managers for safe connection handling
- shared Database instance across plugins
- automated test suite
- ruff linter
- web interface as second entry point
- transport-namespaced user identity
- transport-independent plugin commands
- notification channel layer
- database health endpoint
- shared-secret access control on the web interface
- rate limiting on the public forms
- security headers on every web response
- username restrictions and failed-login logging
- CSRF tokens on the state-changing forms
- audit logging of sign-in, sign-up, sign-out and Telegram link changes
- notification text withheld from the web logs
- account administration from the host (disable, enable, reset a password,
  revoke sessions)
- deployment runbook
- automatic deploy on push to `main`, over SSH from GitHub Actions
- an administrative area at `/admin`, one boolean on the account, granted from
  the host
- accounts, search by name, and enabling and disabling from there
- the visit log, and a test that keeps deployment details out of the repository

## Deployment

A push to `main` runs `scripts/deploy.sh` on the host: `git pull --ff-only`,
`uv sync --frozen --no-dev`, restart, journal tail. No third-party action, no
self-hosted runner — the host carries one script and one key.

The deploy runs as `automation`, which owns the checkout, so a pull does not
leave root-owned files behind and no `chown` is needed. Two steps are
privileged, each a sudoers rule with every argument written out: the restart
and the journal read. `automation` is deliberately not in `systemd-journal`,
which would grant read access to every log on the host for ever.

The SSH key is confined to that one script by `command=` in `authorized_keys`,
with `no-pty` so it cannot open a shell that skips it. **That is not the
security boundary.** The script lives in the repository, so whoever can push
to `main` can change what it runs — and pushing code here already means it
runs on this server, because the deploy starts it.

**There is no separation of duties in this project, and the documentation does
not claim otherwise.** Branch protection is configured on `main` with the
owner as an always-allow bypass. It refuses force pushes and branch deletion,
and it covers anyone without admin — a later collaborator, a token — without
anyone having to remember this file. It does not put a reviewer between the
owner and the server: an author cannot approve their own pull request, and
there is no second approver. So the honest boundary is the GitHub account.
The automation removes the waiting, not the permission.

The script pulls and then re-opens itself with `exec`, because the pull can
rewrite the file it is running and bash reads scripts by offset from an open
handle. A script that overwrites itself with a shorter one prints its first
line and stops — a deploy that pulls, syncs, never restarts, and reports
success. The pull is inside a guard so the second pass cannot reach it.

Server setup is in DEPLOYMENT.md and is not in the repository.

## Current architecture

Entry points

Telegram (aiogram)          Web (aiohttp)
     │                           │
     └──────────┬────────────────┘
                ▼
        PluginManager
                │
                ├── Command dispatch
                ├── Plugin lifecycle
                └── Plugin management
                        │
                        ▼
                    Plugins
              execute() — transport-independent
                        │
                        ▼
               Repository layer
                        │
                        ▼
                 MigrationRunner
                        │
                        ▼
                   PostgreSQL

Notification delivery

Plugin → Notifier → resolver picks the target → channel by kind
                        │
              ┌─────────┴─────────┐
        TelegramChannel        WebChannel
                                (stub)


## Core components

Core

## Responsibilities:

* application startup
* configuration loading
* logging
* Telegram bot initialization
* web interface initialization
* notification channel registration


## Plugin system

## Implemented:

* BasePlugin contract
* PluginManager
* plugin registration
* plugin metadata
* command metadata
* plugin lifecycle
* transport-independent execute()
* automatic command routing

Plugins implement `execute(command, args, identity)` and return a
`CommandResult`. They contain no transport-specific code. The platform
dispatches declared commands on every transport automatically.

`router()` is optional and reserved for handlers a command cannot express,
such as inline keyboards or file uploads.

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
* /status

⸻

Notes

Purpose:

* personal notes storage

Implemented:

* create notes
* persistent storage
* PostgreSQL repository
* read across a linked Telegram pair, from either transport
* /notes numbers the list, /del removes one, /clear removes all
* notes are addressed by position, never by row id
* deletion spans a linked pair too, so a visible note is removable

⸻

Reminders

Purpose:

* scheduled notifications

Implemented:

* create reminders
* PostgreSQL storage
* background worker
* self-managed lifecycle
* delivered to the linked Telegram chat, not dropped in the browser
* an undeliverable reminder is given up on after three passes rather than
  retried for ever; a raising delivery is retried indefinitely

⸻

Clipboard

Purpose:

* personal temporary text storage

Implemented:

* /copy <text>
* /paste
* PostgreSQL repository
* one buffer across a linked pair — copy on the web, paste in the bot

⸻

Expenses

Purpose:

* track personal spending, with the category list the account creates for
  itself

Implemented:

* a page of its own at /expenses, reachable without typing a command
* categories created, listed and retired from the page; no code change
* amounts are Decimal against a NUMERIC column, rounded only for display
* spent_at is the day the money was spent, so a month follows the spending
  and not the entry
* monthly breakdown by category, a bar chart and a figure table built from
  one aggregation, and a comparison against the previous month
* the date field follows the month on screen, and keeps a value the reader
  typed by hand
* web-only: the router refuses the command in a chat
* reads widen across a linked pair; writes do not

⸻

Applications

Purpose:

* track job applications and see where the pipeline narrows

Implemented:

* a page of its own at /applications, on the same base as the spending page
* statuses are a state machine: each stage declares which stages may follow
  it, the row's buttons are drawn from that declaration and the server
  enforces it, so a move that is not offered is a move that is refused
* the funnel is one group-by; each rung counts the stage and every stage
  after it, as a share of everything ever sent
* rejections are reported beside the funnel rather than drawn as a rung
* silence is counted only while an answer is outstanding, from five days;
  age stays on every row
* a date in the future is refused
* web-only; reads widen across a linked pair, writes do not

⸻

## Web interface

Purpose:

* browser access to the same command set

Implemented:

* GET /login, POST /login — sign in form
* GET /signup, POST /signup — registration; 404 while SIGNUP_INVITE_CODE is unset
* POST /logout — ends the session
* GET / — HTML console
* GET /about, GET /project — static public documents, no session needed
* GET /api/commands — plugin and command metadata
* POST /api/command — command execution
* POST /api/page/action — a plugin page reporting what was done, answered
  with the page redrawn
* a route per plugin page (e.g. /expenses, /applications), built from what
  plugins declared
* GET /demo — public demonstration: landing, plugin pages and actions,
  reachable without a session
* GET /health — database health check
* WEB_HOST / WEB_PORT configuration

Demonstration:

* /demo, /demo/expenses, /demo/applications and POST /demo/api/action are
  public; the rest still needs a session
* a visitor is `web:guest:<token>` from a cookie — never a row in `users`,
  never a password, never a session
* a guest namespace is seeded on arrival, so the cleanup can run at any time
* writes are capped per guest
* the page states that the data is invented
* /about links to it, so a reader arrives from a résumé without being asked
  to register
* `python -m app.manage prune-guests` deletes every guest namespace

Access control:

* reverse proxy terminates TLS and injects the X-Platform-Auth secret
* X-Platform-Auth shared secret, verified as middleware covering every route
* session cookie (HttpOnly, SameSite=Lax, Secure) issued at /login
* every route except the two auth pages, the two public documents (/about,
  /project) and /health requires a session
* WEB_HOST binds to loopback, port 8080 never exposed
* /login and /signup are rate limited: an in-process sliding window keyed on
  X-Forwarded-For (login also per username, cleared by a successful login),
  429 once the allowance is spent
* every response carries the security headers — CSP restricted to the app
  origin plus https://telegram.org, X-Frame-Options DENY, nosniff,
  Referrer-Policy no-referrer; Strict-Transport-Security only when
  SESSION_COOKIE_SECURE is set
* usernames are restricted to [A-Za-z0-9._-], at most 32 characters, and
  failed logins are logged
* the state-changing forms carry a CSRF token (`app/web/csrf.py`): the two
  anonymous forms use a double-submit cookie (HttpOnly `platform_csrf`, echoed
  into a hidden field) and the session-backed forms (sign out, unlink) derive
  a token from the session; a forged anonymous submission re-renders the form
  with 400, a forged session-backed one is 403
* sign-in, account creation, sign out and Telegram link/unlink are written as
  audit events, and the text of a web notification is never logged

An account can be paired with a Telegram chat, stored in `telegram_links` and
one-to-one in both directions. The pair never moves data: web rows stay under
`web:<name>`, Telegram rows stay under `telegram:<id>`, and neither identity is
merged into the other.

From the console, through Telegram's Login Widget: the page shows either a
link button or the linked chat with an unlink button, never both. Requires
`TELEGRAM_BOT_USERNAME` and a domain registered with `/setdomain`; without
both, no button and no route.

Implemented:
- GET /account/telegram/link — verifies the signed payload, binds it to the
  browser that started the flow with a one-use nonce, rejects payloads older
  than TELEGRAM_LOGIN_MAX_AGE_SECONDS
- POST /account/telegram/unlink — removes the pairing
- verification errors are deliberately vague; the reason is logged instead
- re-linking to a different chat replaces the old one and logs it; re-linking
  to the same one is a no-op rather than an error
- a chat already held by another account is refused and named

Managed from the host with `python -m app.manage link-telegram <username>
<chat_id>`, `unlink-telegram <username>` and `list-links`, which is also the
way out when the browser cannot reach Telegram.

The pair also widens reads. `PersonResolver.identities()` returns the identity
plus whatever it is paired with, and the notes and clipboard plugins pass that
set to their repositories:

* notes are read across the pair in both directions, so a note written in the
  bot appears in the console and vice versa, as one list in real order
* writes stay under the identity that wrote them — the stored row still
  records where it came from, and unlinking is a single delete rather than a
  row-by-row move back
* the clipboard is one buffer rather than a history, so `/paste` reads across
  the pair and the last write wins whoever made it

An identity with no pair resolves to itself, so an unpaired deployment behaves
exactly as it did before.

Accounts are created either from the host with `python -m app.manage
create-user`, or through the sign-up page when `SIGNUP_INVITE_CODE` is set.
Passwords are stored as salted scrypt hashes; only the SHA-256 of a session
token is kept. Two web users cannot read each other's data.

Administration beyond creation is done from the host, where a leaked credential
is dealt with without a browser:

* `disable-user <username>` flips `is_active` off and revokes the account's
  live sessions; `enable-user` turns it back on. Sign-in and every request are
  already refused for an inactive account, so disabling takes effect at once.
* `reset-password <username>` reads a new password with `getpass` (never as an
  argument) and revokes every session, on the assumption that a password worth
  changing is a password worth assuming is known.
* `revoke-sessions <username>` ends every session without touching the
  password, which is the response when only a session leaked.

None of them delete data: a disabled account keeps its notes, reminders and
clipboard until it is enabled again. There is no `rename`: an account's data is
keyed by `web:<username>`, so renaming would have to move rows across notes,
reminders and clipboard, and every future plugin would have to remember to join
in. That is deferred until identity is anchored to something other than the
name.

Identity resolution lives in app/web/auth.py and maps the session cookie to
`Identity(WEB, username)`. That function remains the seam for user accounts —
no plugin or repository knows about any of this.

Deployment runbook: docs/DEPLOYMENT.md

⸻

## Notifications

Purpose:

* deliver a message to a user over the transport they arrived on

Implemented:

* NotificationChannel interface
* TelegramChannel — sends through the bot
* WebChannel — placeholder that logs the recipient and message length, never
  the text
* Notifier — routes by identity kind, and returns whether anything was sent
* LinkedChatResolver — a web account's notification is delivered to its paired
  Telegram chat; the only place where delivery crosses transports

⸻

## Database

Current database:

* PostgreSQL
* common database.py for all plugins
* SQL migrations support
* context managers for connection safety
* shared Database instance across all components
* indexes on user_id columns for all tables
* user_id is TEXT namespaced as kind:id

Database schema:

sql/migrations/
├── 001_initial.sql
├── 002_add_indexes.sql
├── 003_user_identity.sql
├── 004_users_sessions.sql
└── 005_telegram_links.sql

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
* All repositories use context managers to prevent connection leaks.
* Database schema changes are managed through SQL migrations.
* Migrations run automatically during application startup.
* Applied migrations are tracked in schema_migrations table.
* Migrations are wrapped in transactions with rollback on failure.
* Timestamps use UTC consistently (NOW() AT TIME ZONE 'UTC').

app/db/database.py

⸻

## Current limitations

* A visit is recorded without an address, deliberately. That makes the owner's
  own checking indistinguishable from a stranger's in the log: the row says a
  page was looked at and nothing about who looked. The page can hide the
  administrator's authenticated sessions, and it does, but anonymous traffic
  from the owner's own address is counted as somebody else's. Anyone reading
  the numbers should know how much of the day is the owner checking the site.
* Sign-up is gated by one shared invite code with no approval step behind it —
  whoever holds the code can join.
* Web notifications are still dropped rather than delivered, but only for an
  account that has not linked Telegram. A linked one reaches its chat.
* Telegram linking needs a domain registered with BotFather, so it cannot be
  tried on localhost — the button stays hidden until that is done.
* /clear removes every note at once with no confirmation, which is right for a
  single-user tool and wrong the day there is a second one.
* A web account cannot be renamed: its data is keyed by the username, so a
  rename would have to move rows across every table that stores a `user_id`.
* No external integrations. The demonstration is the only thing that writes
  without a session, and it is capped and rate limited rather than closed.
* Web console is functional but minimal.
* The demonstration namespace is pruned by a systemd timer on the host, daily
  at 05:23. This is a place where the repository and the machine can
  disagree, and it did — see the note below about what the docs miss. The
  visit log has the same shape: a command and a window in here, a unit on the
  host that is not installed.
* Guest writes are capped (12 categories, 100 expenses, 25 applications), and
  the whole demonstration is rate limited to 200 requests an hour per address.
  Neither is a wall: a visitor with proxy addresses gets past the limit, and
  the caps are per namespace, so a determined attacker can still add rows.
  Both raise the price rather than forbid it, which is the right size of
  answer for a personal site and should be said as such.
* The interface is Russian and there is one language, not a switch. Adding a
  second means an i18n layer that does not exist yet; the strings are already
  in one place per template, which is what such a layer would need.
* `app.manage` output is still English on purpose — it is read in a shell and
  in the journal, not in a browser.
* A migration is applied only after a dump, and a failed dump stops the
  application from starting. A host without `pg_dump` therefore cannot boot
  once a migration is pending.
* Routes are registered once, when the application is built, from the plugins
  present at that moment. A plugin registered afterwards has a menu entry but
  no address. Only reachable by a test today; it would matter for anything
  that loads a plugin at runtime.

⸻

## Potential tasks:

* add reminder to /cancel and /reminders listing;
* webhook receiver, so something outside can drive the platform; the cost is a
  public endpoint that writes, which is the same shape as the demonstration
  and wants the same limits;
* anchor identity to a stable key so an account can be renamed without moving
  its data, which is what the deferred rename command needs;
* install the visit-log cleanup timer. `prune-visits` is written, tested and
  documented, and the unit is not on the host — the same gap the guest timer
  once had. It is not worth installing at the current rate: the table took
  eighteen rows on its first day and the retention window is ninety, so
  nothing is anywhere near needing it. Install it when the table reaches a few
  thousand rows, when a search over old visits is wanted, or when the rows
  show up in how long a dump takes — whichever arrives first.

On the documents themselves: `PROJECT_STATE.md` is edited at the end of a
sprint, which means it describes the code as of that moment and nothing about
the machine. Two entries here had already gone false — the applications plugin
was listed as unbuilt, and the guest cleanup as never scheduled — and neither
was caught by a test. The public page `/project` had gone false the same way,
and more visibly. Anything that depends on the host (a timer, an env var, an
installed binary) is a claim that can rot silently; check it against the server
before trusting it.

⸻

## Development rules

* Keep core independent from plugin internals.
* New functionality should be implemented as plugins.
* Keep business logic transport-independent.
* Avoid unnecessary architectural complexity.
* Prefer incremental improvements over large rewrites.

## Development workflow

Before starting a new sprint:
- Read PROJECT_STATE.md
- Check DECISIONS.md
- Do not rely on previous chat history as source of truth

During implementation:
- Always specify full file paths
- Do not create new files without agreement
- Keep changes minimal
- Run ruff check and pytest before committing
