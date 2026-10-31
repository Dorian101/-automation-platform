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
docs/DECISIONS.md for why `basicauth` was removed. Sign-up is governed by the
single `SIGNUP_OPEN` flag, and when it is false the sign-up page does not
exist at all.

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

`Database` is a connection pool (`psycopg_pool`): `connect()` borrows a
connection and returns it on the way out of the `with` block, committing or
rolling back as the block dictates. The pool opens lazily and is capped by
`DB_POOL_MAX_SIZE` (20), so a request rate spike cannot open connections
without limit; `DB_POOL_TIMEOUT_SECONDS` (5) makes a request fail fast when
the pool is full. Still count queries and never let a loop call a repository —
the pool removes the per-connection cost, not the per-row one.

Repositories:
- notes_repo.py
- reminders_repo.py
- clipboard_repo.py
- links_repo.py
- users_repo.py
- sessions_repo.py
- expenses_repo.py
- applications_repo.py
- visits_repo.py

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
- `demo` — whether the page belongs on the public demonstration. **Default
  off.** See "The demonstration" below.
- `command_args` — the fields a command with arguments needs, keyed the same
  as `commands`. Each field is a `(type, label)` pair, `type` being `text` or
  `number`; the label is the prompt drawn under the input. A command absent
  from it takes no argument and runs on a click; a command present reveals a
  labelled form whose values are joined by spaces into `args`.
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

A form's row is a `<div>`, not a `<form>`, so there is no implicit Enter-to-
submit; the renderer restores it by listening for Enter on the row and firing
the submit button, skipping `select` fields where Enter means "pick an option".
A `<input type="date">` opens the browser's calendar on any click — a plain
click by itself only works on the field's icon in some Windows setups, so the
renderer calls `showPicker()` to make the whole field behave the same.

## Current plugins

- Notes: /add, /notes, /del, /clear
- Reminders: /remind, /reminders, /cancel; delivers through notification channels
- Clipboard: /copy, /paste
- Expenses: page at /expenses, web-only
- Applications: page at /applications, web-only, statuses are a state machine
- Tasks: page at /todo, web-only, a kanban board with deadline notifications
- System: /plugins, /help, /status

## Statuses as a state machine

`STAGES` in the applications plugin is the single declaration: each stage
carries its label and the stages it may move to. The buttons on a row are
drawn from it and `_set_stage` checks against it, so the page cannot offer a
move the server would refuse, and a stale page cannot talk the server into
skipping a stage. `offer` and `rejected` are terminal.

The funnel is one `counts_by_status` group-by, and the arithmetic that matters
is that a rung counts the stage **and every stage after it**. A status is the
stage an application is in now, so an application at `interview` has already
been answered; counting occupied stages reported 2 answered out of 2 still
waiting — a hundred percent out of four where half had progressed. The
denominator is everything ever sent, rejections included, because a rejection
is an outcome of having applied; excluding it flatters every later stage.

A repository that enforced transitions would be deciding something about the
feature rather than about storage. It stores and reads; the schema's CHECK is
what keeps a stored stage real on any path that writes one.

## Notification layer

`Notifier` routes a message to the channel matching the target's transport.

- TelegramChannel sends through the bot
- WebChannel is a placeholder that logs the recipient and the message length
  but never the text, so a notification's contents do not land in journald,
  pending a web inbox

Adding a transport means adding a channel, nothing else.

`Notifier.deliver` returns whether the message actually went anywhere, so a
caller can tell "delivered" from "handled but undeliverable" — `TelegramChannel`
returns true after a real send, `WebChannel` returns false. An optional
resolver answers "where should this reach, given who asked", and may return an
identity naming a different transport than the one the request arrived on:
`LinkedChatResolver` sends a web account's notification to its paired chat, and
falls back to the web identity when there is no link. That is the only place
that decides delivery crosses transports.

A reminder that has nowhere to go is refused at create time rather than left
to fail later: the reminders plugin asks `Notifier.destination` before writing
the row, and a web account with no Telegram link gets a message telling it to
link first. A reminder that is already due with nowhere to go is given up on
after three passes rather than retried for ever, and a delivery that raises is
retried indefinitely — that is an outage.

Reminder times are rendered in `DISPLAY_TIMEZONE` (`_format_remind_at`, via
`ZoneInfo`), the same setting `/admin/visits` uses, not in the host's own zone —
the box keeps UTC and the reader sees their own wall clock. The setting defaults
to UTC, so a deployment that never sets it stays on UTC; this one names Moscow
to read as MSK.

## Web interface

An address nothing serves gets `templates/not_found.html` with a 404, and so does
a non-admin asking for the administration, so the two answers are identical. A
missing demonstration page gets the same page.

`/project/trace` runs one of the fixed `TRACE_SCENARIOS` and returns the stages
it passed, for `app/web/project_page.js` to draw. It is public and limited per
address, takes no command text, and nothing it runs writes. The notification
stage of the reminder scenario is drawn, not executed.

`/project` keeps both reading depths in the markup as `.lens-brief` and
`.lens-deep`. A claim about the platform changes in both places. Without the
script only the engineering text shows.

- GET /login, POST /login — sign-in form
- GET /signup, POST /signup — registration, 404 while SIGNUP_OPEN is false
- GET /about, GET /project — static public documents
- POST /logout — ends the session
- GET / — HTML console
- GET /api/commands — plugin and command metadata
- POST /api/command — execute a command
- The console's command list is rendered as clickable chips. A click puts the
  command into the input box with a trailing space, so a command with arguments
  is one click plus the text and a command without arguments runs as-is. The
  chips reuse the `/api/commands` metadata — no plugin declares a signature for
  this, and no server change was needed.
- POST /api/page/action — a plugin page reporting what was done; the target
  page is named in the body because the endpoint is shared, and the answer is
  the page description again
- one GET route per declared page (e.g. /expenses), built at app start
- GET /demo, /demo/{page}, POST /demo/api/action — the public demonstration,
  reachable without a session
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
  username; a successful login clears its own key), answered 429 when spent.
  Login carries a **second window counted per address across every name**,
  checked *before* the per-user one, so a fresh username is not a fresh
  allowance; a refused attempt does not create the key it was refused on,
  which bounds the store
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

## Search engines

`robots.txt` and `sitemap.xml` are routes, not files: there is no `sitemap.xml`
on disk and `find` will not find one. Both are generated per request by
`_robots` and `_sitemap` in `app/web/server.py`, from the same
`INDEXABLE_PATHS` tuple beside them.

**Adding a public page means adding its path to `INDEXABLE_PATHS` in
`app/web/server.py`.** That is the one place to remember, and nothing will
tell you if it is forgotten: the page renders, every test passes, and the
crawler simply never hears about it.

Why generated rather than a file: the list of addresses would then live in two
places — the file and `PUBLIC_PATHS` — and drift, the way five hand-written
copies of the navigation once did. `lastmod` is today's date for the same
reason a static file goes stale without anyone touching it. The host name comes
from `PUBLIC_BASE_URL`, never from the `Host` header: the app sits behind the
reverse proxy, and that header is not the public name of anything.

The sign-in and registration pages carry `noindex`. The rest of the site needs
a session and gets nothing. `/` redirects to `/login`, which is why that form
carries the only links out to the public pages — **remove them and the whole
site becomes unfindable again**, silently. `tests/test_seo.py` covers the
crawl path end to end for exactly that reason.

The domain itself still has to be registered with Google Search Console and the
sitemap submitted there; nothing in the code can do that part.

The share preview (`og:image`) is `/og-image.png`, the only binary asset in the
repository and the one thing a messenger commonly falls back to when there is no
image at all. It is drawn in `app/web/og-image.svg` and rasterised to
`og-image.png` (1200×630), because the parsers that render `og:image` do not
render SVG. Change the look in the SVG and re-render the PNG — never edit the
PNG by hand. Both the favicon and this image share one mark: a hub with modules
radiating off it, the platform's shape, not a chat bubble.

The `/about` hero is a canvas drawn by `app/web/about_hero.js`, served at
`/about-hero.js` as a file because the CSP allows scripts from this origin only.
Each point is one ticket of an invented SLA report; the counts are in the script
and the page labels them as an example. It runs once on load, whatever
`prefers-reduced-motion` says, and a replay button stays under it.

## Navigation

One navigation, rendered into every page by `pages.nav()`, built from what the
plugins declare. Five hand-written copies of the menu block is how the one on
the spending page lost its link to the applications page while the one on
`/about` kept it: nothing but a copy to forget.

Rendered server-side rather than fetched, so the links are there with
scripting disabled. `GET /api/pages` existed to feed the fetched version and
was removed with it.

A `<details>` that opens itself. No state, and a menu that costs the same space
whether there are four sections or twenty. One tiny inline script (guarded by
the CSP nonce) closes it when the reader clicks anywhere outside it — a native
`<details>` has no such gesture, so without it a stray click leaves the menu
open until the toggle is pressed again.

A guest is offered a different menu: the plugin pages point at the
demonstration's copies, because `/expenses` answers a visitor with a login
form. The demonstration is the only part of the site reachable without
signing in, so walking a reader from it into a wall is the last thing its menu
should do.

## The demonstration

`/demo` is the only part of the site reachable without a session, and it is
reachable because a reader from a résumé has no reason to register first.

A visitor is `web:guest:<token>`, built from a cookie and nothing else. Never a
row in `users`, so cleaning up is a delete by prefix. A username cannot
contain a colon, so the prefix cannot collide with an account by construction.

**The invariant: nothing in `app/web/demo.py` may resolve a session.** The
handlers take the identity from the cookie. If any of them reached for
`resolve_identity`, a visitor could run commands as a real account — the one
place on the site that must not be able to. A test reads the module source and
fails on the appearance of a call; it is crude and it is the cheapest guard
against a hole that would not otherwise show up as a failure.

**A page is published only if its plugin sets `demo = True`.** The default is
off and the default is the point: reaching a public URL without anyone
deciding it should is the mistake, and deciding is one line. Three places
honour it — `/demo` drops the page from its cards, a guest's menu omits it,
and `/demo/<page>` answers `404` — and the last is the one that matters,
because a link being absent does not make a page unreachable. The owner's own
menu and address are unaffected: private to the public, not gone.

Namespaces are seeded on arrival, so `prune-guests` can run at any time. The
seed is the plugin's own `seed_demo`.

Two limits, and the reason for both is that **a namespace is defined by a
caller-controlled cookie**. The caps inside the plugins are therefore per
namespace — per visit, not per visitor, since minting a cookie costs nothing.
And a namespace is seeded on arrival, so a plain page load writes about two
dozen rows with no POST involved. Hence the rate limit, applied to the whole
demonstration rather than to the actions only, and keyed on the address rather
than the guest for the same reason. 200 an hour, which is about an order of
magnitude above a real visit.

Neither limit forbids anything. They raise the price of abuse, and that is
the right size of answer here — say so rather than claiming protection.

## Migrations and the dump

`MigrationRunner` takes an optional backup manager. `app/main.py` passes one,
so a deploy carrying a migration dumps the database first; nothing is dumped
when nothing is pending. One dump per run rather than one per file, because
what is wanted on a failure is the state before any of them landed.

A failed dump **stops the run**. The database is not touched and the
application does not start. A host without `pg_dump` therefore cannot boot
once a migration is pending — the deliberate cost of not applying a schema
change with no way back.

Bound to `WEB_HOST` and `WEB_PORT`, defaulting to 127.0.0.1:8080.

## Deploy

A push to `main` runs ruff and pytest on a runner and then, if both pass,
`scripts/deploy.sh` on the host via `.github/workflows/deploy.yml`. The script pulls with `--ff-only`, runs
`uv sync --frozen --no-dev`, restarts the service and prints the journal tail
through the two sudo rules,
and prints the last 50 journal lines.

Two things about it are easy to get backwards:

- **The restricted key is not the security boundary, and neither is branch
  protection.** The key is confined to one script, but that script is in the
  repository, so whoever can push to `main` can change it — and pushing code
  here already means that code runs on the server, because the deploy starts
  it. A ruleset on `main` refuses force pushes and branch deletion for
  everyone, the owner included, but it does not put a reviewer between the owner and the
  server, because there is no second reviewer and an author cannot approve
  their own pull request. The boundary is the GitHub account itself. Saying
  otherwise here would be the comfortable lie, and this file is what gets
  read when the comfortable version is more convenient.
- **The deploy runs as the user that owns the checkout**, not as root, which
  is why it needs no `chown` after the pull. Two steps are privileged — the
  restart and the journal read — and each is a sudoers line spelled out to the
  argument, with no wildcard.

  `automation` is deliberately not in `systemd-journal`. Membership is one
  command instead of a rule and grants read access to every log on the host,
  permanently. The two rules read one unit's last 50 lines and stop.

  sudoers matches arguments exactly, so `scripts/deploy.sh` and the rules in
  DEPLOYMENT.md have to agree on `-n 50`. A test checks it. Change one without
  the other and the deploy breaks, but in the runbook rather than in
  production.

The script pulls and then hands over to itself with `exec`, because the pull
can rewrite the file it is running. Bash reads a script by offset from an open
handle; a script that overwrites itself and gets shorter prints its first line
and stops. A deploy that does that pulls, syncs, never restarts, and reports
success. The re-open is guarded by an argument, and the pull sits inside the
guard so the second pass cannot reach it.

`pyyaml` is a dev dependency for one thing: parsing the workflow. A malformed
workflow is the quietest failure in the repository — nothing runs and nothing
says so. `tests/test_deploy_automation.py` reads both files, and checks the
script's *commands* rather than its text: the script explains `--ff-only` in a
comment, and a search over the whole file finds the explanation whether or not
the command is still there. Which is how an earlier version of that test
asserted a merge-prone pull was safe.

`automation` also needs its own SSH key for GitHub: the deploy runs `git pull`
as that user, not as root, so the key root already has does not help it. It is
a read-only deploy key — `git pull` only reads, and the server's write access
through root is not something to widen.

Server-side setup — the `automation` shell, the sudoers rules, the authorized_keys
line and the pinned host key — is in DEPLOYMENT.md, not here. It is not in the
repository because it is not in the repository.

## The administrative area

`/admin` is a sub-application in `app/web/admin.py` with a middleware in
front of it, and that shape is the point. The check is not a line in each
handler: a route added to the sub-application later is covered without
anybody remembering, and a route registered on the main application is simply
not under `/admin`. A check that has to be repeated per handler is a check
that eventually is not repeated.

**Not a plugin.** A plugin is a unit of user functionality with its own
commands, tables and page; this acts on the platform, across every plugin's
data, and belongs to the deployment. Putting it in `app/plugins/` would have
meant a plugin that touches tables it does not own and a `BasePlugin` flag
whose only job is to mark a page as privileged.

**One boolean, not a roles table.** There is exactly one question asked of it:
may this person open the pages that act on the platform rather than on their
own data. The console already answers for everything else — it is behind a
sign-in form, which is the "authorised user" role without the name. A second
kind of person can appear later, and the table can be added then, with
something to point at.

**Read from the database on every request**, not from the session, so
`revoke-admin` closes the door at once instead of at the next sign-in. That is
also why it leaves sessions alone: there is nothing to revoke.

**Both refusals copy the application rather than inventing.** No session: 303
to `/login`, the answer every other address gives. Session but no flag: 404,
the answer every address that does not exist gives. `/admin` is therefore not
a thing a request author can distinguish from a typo, and `/ADMIN`,
`/admin/` and `/admin/anything` are all refused as well.

**A guest is not an exception to handle.** A guest has no row in `users` and
so cannot hold the flag, and the administrative page is not on the
demonstration. Same arrangement one level down from `demo`: the link is in
the menu for whoever holds the flag and absent for everybody else, and hiding
a link is not what keeps the page closed.

### What lives under it

`/admin` leads to `/admin/status`, the overview: disk, memory and load from the
host, per-table row counts, and the newest backup. A landing page for an area
with one section is a page whose only content is a description of itself, and
its reader does not need it — now that there are three sections the overview is
the front door, the page that answers "is the platform healthy" without a
click.

The status page draws its frame once and its numbers from a JSON endpoint
(`/admin/status/data`) that a small script refreshes every thirty seconds, so
watching the disk does not mean reloading the page. The system figures come
from `app/web/system.py`, which reads `/proc` and `os.statvfs` rather than
`psutil` — a dependency for three numbers is not needed, and the sandbox
already keeps `/proc` readable. A figure that cannot be read is `None`, which
the page renders as `—`, never a fabricated zero.

The search is a GET and the actions are POSTs with a token. A search changes
nothing, and an address that can be bookmarked is worth more here than a POST
that cannot be.

Two things about the actions:

- **An administrator cannot disable their own account.** Otherwise the only
  place that could undo it is the one just locked. The flag survives, so
  `enable-user` from the host is the way back, but a mistake in here should
  not be what does it.
- **Nothing revokes sessions on disable.** A disabled account stops working
  on its next request, because that is checked on the way in.

The id in the action routes is matched against digits in the route itself, so
`/admin/users/alexey/disable` stops before a lookup rather than at one.

`UsersRepository.search` escapes the LIKE wildcards. Unescaped, a search for
`%` returns the whole table, and a search box that answers differently
depending on the punctuation of the query is not something anybody can reason
about later.

**One `CsrfSigner` for the whole web layer**, passed into the area rather
than built inside it. Two signers meant two secrets and two answers to the
same question, and the failure would have read as a forgery rather than a
wiring mistake. It also has to be the same across restarts: the secret comes
from `CSRF_SECRET`, and without it every form open at the moment a deploy
goes out is refused with `Форма устарела` and no visible reason.

**A refused CSRF token redirects; it does not answer 403.** A token belongs to
the session it was drawn for, so it stops matching the moment that session
ends — which happens on its own when somebody signs out in one tab and signs
back in, or signs in as somebody else. Nothing was forged and the reader did
nothing wrong, and the answer used to be one line of plain text on a black
background with no way out. The action is still refused; the browser is just
sent somewhere a fresh token is waiting, and the refusal is left in the log
where a reader will not mistake it for their own mistake.

### Visits

`/admin/visits` is a log of what happened on the public side: a view of a
public page, a sign-in, a sign-up. Newest first, filterable by page.

**No address, no fingerprint, nothing that names a stranger.** A reader with
no session is recorded as having arrived and nothing more; a demonstration
guest is recorded under a random token that resolves to no account. An
address would have told the owner nothing they did not already have — the
useful part of a visit is when it happened, and "who was that" is answered by
lining the time up with a résumé site, not by having collected something to
correlate against.

**The referrer is an origin, never a URL.** When a browser arrives across a
link it sends a `Referer` header, and the log keeps only the origin —
`https://example.com` — out of it. The path and query are dropped before the
row is written, so a search engine's referrer says which engine and not what
was typed into it. A direct visit, a bookmark, or a browser that withholds the
header leaves the column NULL, which reads as no referrer rather than as an
empty string that looks like one. The shortening lives in `_referrer_origin`,
one place, so every writer of a visit row passes through it.

**One writer, a middleware, not a line in each handler.** `_is_a_visit` says
what counts: a GET of a public page that answered 2xx or 3xx. The console is
deliberately left out — it is where the owner works, it would bury everything
else, and it is already known. Sign-in and sign-up are recorded by the
handlers that succeed at them, because the moment they happen is not the
moment the request arrives.

**A demonstration guest's identity comes from the handler, not from the
request.** The guest token is minted in the handler and set as a cookie in
the response; a first-time visitor has no cookie on the request either, so
reading it after the fact would mint a second token and file the visit under
a namespace that never existed. The handler leaves the identity on the
request, and the log reads it from there.

**The landing mints a guest too**, so one reader is one token from the front
door onwards. It used not to, so that somebody who only listed what there was
to look at would not leave a namespace behind — but a token is a cookie, and
namespaces are written by `_view`, which the landing does not call. Arriving
cost a cookie and a row and nothing else, while what it saved was nothing: the
arrival was filed as nobody and the same reader's next click under a token, so
one person was two rows with nothing joining them. Came and left without
clicking still shows afterwards — a token on `/demo`, appearing once, on its
own.

**Hiding the administrator asks who they are once, not once per row.** The box
drops the owner's own events, and the identity is the same for every row — so
it is resolved before the filter, outside it. Called inside, it was two
queries and two connections per row: four seconds at two hundred events, with
nothing on the page individually slow enough to notice.

**The exclusion is asked for in the query, not applied to the rows.** It used
to be a Python filter over the rows the page had already chosen, which put two
different numbers in the room: the rows on the page, and the rows the page was
cut from. The count and the table are now cut by the same line — `count` takes
the same exclusion as `recent` — so the total is the number of rows the log
holds for other readers, and it matches the table under it. The exclusion is
written `IS DISTINCT FROM` and not `!=`, because a visit by a reader with no
session has no identity at all and `NULL != 'web:somebody'` is not true in
SQL; that would have dropped every anonymous row from the count the moment
the owner asked to be hidden.

**The log is read a page at a time.** `page=2`, fifty rows to a page, and a
page number past the last is clamped to the last rather than answered with an
empty table — a stale link lands on real rows. The filter and the box travel
with the pager, because a reader who narrowed the log and then turned the
page expects to still be looking at the same narrowing.

**The count line says three things and all three are true:** how many events
the log holds, how many of them the filter keeps, and which of those are on
the screen. The filter's number only appears when it differs, because a page
with no filter has one total and not two of the same.

**Two ways out of the log, and both are compared on the way in.**
`VISIT_IGNORE_IPS` matches the address the reverse proxy wrote, which covers
the machine the owner browses from and nothing else. `VISIT_PROBE_TOKEN`
matches the `X-Platform-Probe` header, which travels with the request and so
covers work done from anywhere — an address filter alone is useless for that,
because measuring the site arrives from every address in turn. Both live in
`_leaves_no_trace`, which the view, sign-in and sign-up paths all ask.

Neither keeps a determined visitor out: the address list is a list, and the
token is a header anyone who has it can send. The log is not an audit trail
and was not built as one.

**Repeat visits are a query, not a column.** The table is an event log;
counting rows for a token answers "how often" without a second mechanism that
can disagree with the first.

**`prune-visits` from a timer, the same shape as `prune-guests`** — the
command is written, tested and documented, and the timer runs daily at 05:47
on the host. It is the only thing that removes from the table, which is
written on every public request.

**Not here on purpose:** creating accounts and resetting passwords. A person
signs up, or resets their own password, through the ordinary forms. The CLI
commands for both stay for the cases where a browser is not available, which
is the reason they were written.

## Development rules

The working rules and the release order live in a local, untracked `CLAUDE.md`.

## Runtime

Application is launched in linux daemon with:
uv run python -m app.main

Production deployment uses systemd service on VPS.

Running it locally is the same command and carries one condition: the local
`.env` holds a **development bot token**, never the production one. Two pollers
on one Telegram bot fight, and the production bot stops fetching for as long as
the local process lives — which has happened once, and is why
`check-local-bot.sh` in the sibling `automation-platform.agent/` exists and should
be run first.
Setup and the rest of the local arrangement are in `DEPLOYMENT.md`.

## Tests

Run against a dedicated `automation_platform_test` database, with the schema
built once and emptied per test. Covers identity, command parsing, repositories,
migrations, plugins, the manager, notification channels, the web layer,
accounts and sessions, password hashing, and database backups.

Two modes, because hashing a password in every test was most of the wall clock
rather than anything the change under review touched.

**The suite runs at a cheaper scrypt cost than production.** The shipped one
is 2^17, which costs about a quarter of a second per hash, and at one account
per test it was most of the wall clock — the tests were measuring the password
module. `TEST_SCRYPT_N` in `tests/conftest.py` lowers it for every test, and
`test_passwords.py` keeps one test that hashes and verifies at the real figure,
because otherwise nothing would notice if the shipped parameters stopped
fitting in the memory `_derive` allows.

**The schema is built once for the session and emptied per test.** Nine
migrations applied per test measured 91 ms against 5 ms for TRUNCATE. The
tests that run the migrations themselves take a different fixture that hands
over a genuinely empty database, and `database_with_schema` rebuilds the schema
if it finds it gone, so one such test cannot leave the rest of the run
addressing tables that are not there.

**While working — run only what the change can break.** This is the targeted
run and it is the default during a sprint. Pick by area:

| Changed | Run |
|---|---|
| a plugin | its own `test_<name>_plugin.py`, plus `test_manager.py` |
| a repository | its own `test_<name>_repo.py` |
| a page or `/api/page/action` | `test_web_pages.py`, `test_seo.py`, `test_demo.py` |
| `BasePlugin`, the manager, routing | `test_manager.py`, `test_plugins.py` |
| the web layer or auth | `test_web.py`, `test_auth.py`, `test_web_pages.py` |
| a migration | `test_migrations.py` plus the repository that uses it |

Those three page files together run in about half a minute, against three
minutes for the whole suite. Rewording a template is not a reason to run all
of it — the count grows every sprint, and paying it on every small change is
how a two-minute suite becomes a five-minute one nobody runs.

**Before a commit — run everything.** `uv run ruff check . && uv run pytest`
with no selection. This is what catches a change
reaching something the targeted run never loaded, and skipping it because the
targeted run was green is exactly how a green suite stops meaning anything.

**After a full run, iterate on failures with `-x`.** The first failure is
usually the one that explains the rest; stopping there is faster than
collecting twenty red tests that share one cause.
