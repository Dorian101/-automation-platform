# AI_CONTEXT

## Project goal

Automation Platform is a modular automation system with two entry points:
Telegram and a web interface. Each feature is implemented as an independent
plugin that works identically on both.

## Architecture principles

- Plugin-based architecture
- Repository pattern
- PostgreSQL persistence
- Separation of concerns
- Transport-independent business logic
- Database schema managed through sql/migrations/

## Entry points

The platform exposes the same command set over two transports.

- Telegram through aiogram polling
- Web through aiohttp

Neither transport knows about the other. Both build an `Identity` for the
caller and hand it to `PluginManager.execute()`.

## Identity

Users are identified by `Identity`, stored as `kind:id` in the database
(for example `telegram:-1001234567890`, `web:alexey`).

Namespacing by transport means the same numeric id arriving on two transports
never resolves to the same user.

Web identity resolution lives in `app/web/auth.py`: it reads the session
cookie, loads the account behind it and returns `Identity(WEB, username)`.
Everything downstream — plugins, repositories, notifications — receives that
identity and never sees a password, a cookie or a token.

A web account can be paired with a Telegram chat through `telegram_links`,
one to one in both directions. The pair records that two identities belong to
the same person; it never moves data. Web rows stay under `web:<name>` and
Telegram rows stay under `telegram:<id>`.

`PersonResolver` turns that into the set of identities a read may span.
Reads, deletes and clears are widened across the pair, in both directions, so a
note written in the bot is visible in the console and removable from it. Writes
are **not** widened: a row is stored under the identity that wrote it, so the
history records where each note came from and unlinking is a single delete
rather than a row-by-row move back. The clipboard is deliberately different — it
is one buffer, not a history, so `/paste` reads across the pair and the last
write wins whoever made it.

A note is addressed by its position in the numbered list, never by its row id:
the id is a global sequence, so it would leak how many notes other people have
and give a number to try. `NotesRepository.entries()` returns `(id, text)` so
the plugin can map a position to a row, and `delete()` is scoped to the
identities being read as well as to the id.

The console offers the pair through Telegram's Login Widget, gated on
`TELEGRAM_BOT_USERNAME`. The widget finds the bot by a `script[data-telegram-login]`
element and replaces that script tag with its own button, so both
`data-telegram-login` and `data-auth-url` live on that `<script>` tag — putting
them on a button next to it silently leaves the account with a dead button.
Verification lives in `app/web/telegram_auth.py` and
is two checks more than the signature: the payload must be recent
(`TELEGRAM_LOGIN_MAX_AGE_SECONDS`) and must carry a nonce issued to the browser
that started the flow. The signature proves a real Telegram user signed it; the
nonce proves this browser is the one presenting it.

## Access control

Two independent layers, both required, doing different jobs.

1. `X-Platform-Auth`, a shared secret in `WEB_ACCESS_TOKEN` verified by
   `verify_access()` as middleware on every route. It is injected by the
   reverse proxy and proves the request came through it, not around it. It is
   a transport property: it says where the request came from, not who sent it.
2. A session cookie issued by `POST /login` and checked by
   `resolve_identity()`. It says who the user is, and it is what separates one
   user's notes from another's.

The sign-in and sign-up pages are public, plus `/health`, which must be able to
answer while the database is down, and the two static documents `/about` and
`/project` (they read nothing from the database and render no user data, which
is the only reason they may work without a session).

Accounts exist only in the application. Caddy holds no credentials — see
docs/DECISIONS.md for why `basicauth` was removed. Sign-up is gated by the
invite code in `SIGNUP_INVITE_CODE`, and when that is unset the sign-up page
does not exist at all.

`WEB_HOST` defaults to loopback for this reason. See docs/DEPLOYMENT.md.

## Command flow

1. Input arrives on a transport
2. `parse_command()` splits it into command and arguments
3. `PluginManager.execute()` finds the owning plugin and calls `execute()`
4. The plugin returns a `CommandResult` carrying transport-agnostic text
5. The transport renders the result

Plugins declare their commands in `commands`. The platform routes them
automatically, so plugins contain no adapter code. A plugin overrides
`router()` only when it needs transport-specific handlers, such as inline
keyboards.

## Database layer

PostgreSQL. All access goes through the shared `Database` helper and
repositories using context managers. The migration from SQLite is complete
(DECISIONS 2026-07-16) and the app has been psycopg-only since — moving to
PostgreSQL is done work, never propose it again.

Repositories:
- notes_repo.py
- reminders_repo.py
- clipboard_repo.py
- links_repo.py

Schema is versioned in sql/migrations/.

## Plugin contract

Each plugin exposes:

- name
- version
- description
- commands
- execute(command, args, identity)
- router() (optional)
- on_startup() / on_shutdown()

A plugin may also declare:

- `page` — a web path it owns, or None. `PluginManager` builds the route table
  from what plugins declared; a path no plugin claimed has no route.
- `web_only` — refuse the command in a chat. The check is in the router, so a
  plugin does not inspect `identity.kind`.
- `page_view(identity)` — return a description of the page.
- `page_action(identity, action, payload)` — handle what the page did, return
  the same description with the change applied. Every payload value arrives as
  a string; the plugin converts and validates.

## Pages

A plugin with a page answers with a description, not with markup: a title,
forms, a chart, a table, numbers. The web layer paints what it is told and
knows nothing about the plugin's subject, so a plugin adds a field without the
platform growing a branch for it. The description is embedded as JSON and the
browser paints it, so the first load and every redraw after an action go
through one renderer. Everything reaching the page goes through `textContent`,
so user text is never markup.

Why this and not a vocabulary in the core: the core already carries a rule
that business logic stays transport-independent. Adding a form-and-chart
vocabulary to the core would make every plugin depend on the platform for
something only the web has to understand. `CommandResult.data` is the
lightweight form of the same idea for a command that wants to be structured;
`page` is the full form.

An analytics screen is a page and not a command. Recording a cost through
commands means `/spend 320 еда`, another month means a different command, and
a chart cannot be built from a sentence. That is the terminal problem the
project exists to leave behind, reached from the other direction.

## Current plugins

- Notes: /add, /notes, /del, /clear
- Reminders: /remind, delivers through notification channels
- Clipboard: /copy, /paste
- Expenses: page at /expenses, web-only
- System: /plugins, /help, /status

## Notification layer

`Notifier` routes a message to the channel matching the target's transport.

- TelegramChannel sends through the bot
- WebChannel is a placeholder that logs the recipient and the message length
  but never the text, so a notification's contents do not land in journald,
  pending a web inbox

Adding a transport means adding a channel, nothing else.

`Notifier.deliver` returns whether the message actually went anywhere, so a
caller can tell "delivered" from "handled but undeliverable". An optional
resolver answers "where should this reach, given who asked", and may return an
identity naming a different transport than the one the request arrived on:
`LinkedChatResolver` sends a web account's notification to its paired chat, and
falls back to the web identity when there is no link. That is the only place
that decides delivery crosses transports.

A reminder with nowhere to go is given up on after three passes rather than
retried for ever, because nothing about it can change until the account is
linked. A delivery that raises is retried indefinitely — that is an outage.

## Web interface

- GET /login, POST /login — sign-in form
- GET /signup, POST /signup — registration, 404 while the invite code is unset
- GET /about, GET /project — static public documents
- POST /logout — ends the session
- GET / — HTML console
- GET /api/commands — plugin and command metadata
- POST /api/command — execute a command
- GET /api/pages — plugins that own a page, for navigation
- POST /api/page/action — a plugin page reporting what was done; the target
  page is named in the body because the endpoint is shared, and the answer is
  the page description again
- one GET route per declared page (e.g. /expenses), built at app start
- GET /health — database health check
- GET /account/telegram/link — completes a Login Widget redirect
- POST /account/telegram/unlink — removes the pairing

Transport security:

- every response carries the security headers: CSP locked to the app origin
  plus `https://telegram.org`, `X-Frame-Options: DENY`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`; HSTS is added only when
  `SESSION_COOKIE_SECURE` is on
- `/login` and `/signup` are rate limited by an in-process sliding window
  (`app/web/ratelimit.py`), keyed on `X-Forwarded-For` (login also per
  username; a successful login clears its own key), answered 429 when spent
- state-changing form posts carry a CSRF token (`app/web/csrf.py`): the two
  anonymous forms use a double submit — a random value in the HttpOnly
  `platform_csrf` cookie echoed into a hidden field — and the session-backed
  forms (sign out, unlink) use an HMAC of the session token. A rejected
  anonymous submission re-renders the form with 400; a rejected session-backed
  one is plain-text 403.
- account and link changes are logged as audit events: sign-in, account
  creation, sign out, and Telegram linking and unlinking (the `manage`
  commands log theirs too)
- failed logins and the rate-limit rejections are logged; the username is
  written with `%r` because it is caller-controlled input
- usernames are restricted to `[A-Za-z0-9._-]`, max 32 characters, enforced at
  the one place they enter the system (`UsersRepository.create`) — which also
  caps what can ever appear in a log line spelling a username

Account administration lives on the command line, where a leaked credential is
handled without a browser: `python -m app.manage disable-user|enable-user`
flips `is_active` (and disabling revokes the account's sessions),
`reset-password` reads a new password with `getpass` and revokes sessions, and
`revoke-sessions` ends the sessions alone. None of them delete data. There is
deliberately no rename: an account's data is keyed by `web:<username>`, so a
rename would have to move rows across notes, reminders and clipboard and every
future plugin would have to join in — deferred until identity is anchored to a
stable key.

Bound to `WEB_HOST` and `WEB_PORT`, defaulting to 127.0.0.1:8080.

## Development rules

- Complete one sprint at a time.
- Update PROJECT_STATE.md after every completed sprint.
- Update AI_CONTEXT.md after every completed sprint.
- Prefer small, incremental refactoring.
- Avoid unnecessary complexity.
- Error handling is centralized at Dispatcher level.
- Business logic stays transport-independent.
- Run `ruff check .` and `pytest` before committing.
- If the owner proposes something that would only hurt the work, stop and warn
  instead of starting to execute it right away. State the tradeoff before
  acting, not afterwards, and let the owner decide with the warning in hand.

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

## Tests

Run against a dedicated `automation_platform_test` database, dropped and
recreated per test. Covers identity, command parsing, repositories,
migrations, plugins, the manager, notification channels, the web layer,
accounts and sessions, password hashing, and database backups.

Two modes, because a full run is about two minutes and most of it is scrypt
hashing a password in each test rather than anything the change under review
touched.

**While working — run only what the change can break.** This is the fast mode
and it is the default during a sprint. Pick by area:

| Changed | Run |
|---|---|
| a plugin | its own `test_<name>_plugin.py`, plus `test_manager.py` |
| a repository | its own `test_<name>_repo.py` |
| a page or `/api/page/action` | `test_web_pages.py`, plus the plugin's |
| `BasePlugin`, the manager, routing | `test_manager.py`, `test_plugins.py` |
| the web layer or auth | `test_web.py`, `test_auth.py`, `test_web_pages.py` |
| a migration | `test_migrations.py` plus the repository that uses it |

The four files together are about a quarter of the suite and run in seconds.

**Before a commit — run everything.** `uv run ruff check . && uv run pytest`
with no selection, as AGENTS.md requires. This is what catches a change
reaching something the fast mode never loaded, and skipping it because the
fast mode was green is exactly how a green suite stops meaning anything.

**After a full run, iterate on failures with `-x`.** The first failure is
usually the one that explains the rest; stopping there is faster than
collecting twenty red tests that share one cause.
