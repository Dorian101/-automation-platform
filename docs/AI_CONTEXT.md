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
Reads are widened across the pair, in both directions, so a note written in
the bot is visible in the console and the other way round. Writes are **not**
widened: a row is stored under the identity that wrote it, so the history
records where each note came from and unlinking is a single delete rather than
a row-by-row move back. The clipboard is deliberately different — it is one
buffer, not a history, so `/paste` reads across the pair and the last write
wins whoever made it.

The console offers the pair through Telegram's Login Widget, gated on
`TELEGRAM_BOT_USERNAME`. Verification lives in `app/web/telegram_auth.py` and
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

The sign-in and sign-up pages are the only public routes, plus `/health`, which
must be able to answer while the database is down.

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
repositories using context managers.

Repositories:
- notes_repo.py
- reminders_repo.py
- clipboard_repo.py
- links_repo.py

Schema is versioned in sql/migrations/.

## Current plugins

- Notes: /add, /notes
- Reminders: /remind, delivers through notification channels
- Clipboard: /copy, /paste
- System: /plugins, /help, /status

## Plugin contract

Each plugin exposes:

- name
- version
- description
- commands
- execute(command, args, identity)
- router() (optional)
- on_startup() / on_shutdown()

## Notification layer

`Notifier` routes a message to the channel matching the target's transport.

- TelegramChannel sends through the bot
- WebChannel is a placeholder that logs, pending a web inbox

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

- GET / — HTML console
- GET /api/commands — plugin and command metadata
- POST /api/command — execute a command
- GET /health — database health check
- GET /account/telegram/link — completes a Login Widget redirect
- POST /account/telegram/unlink — removes the pairing

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
