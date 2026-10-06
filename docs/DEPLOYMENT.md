# Deployment

Reference for restoring this system. Every value below is the one actually in
use, not an example. Read "Gotchas" before changing anything.

## Inventory

| | |
|---|---|
| VPS | Ruvds, hostname `ruvds-9bmwh` |
| Public IP | `187.13.70.46` |
| OS | Ubuntu 24.04, PostgreSQL 16.15, Caddy 2.6.2 |
| Repos | `/opt/automation-platform`, owned by `automation` |
| Domains | `automationplatformlebedev.ru` (primary), `automationplatformlebedev.online` |
| NS | `ns1.ruvds.com`, `ns2.ruvds.com` — **both domains** |
| Exposed ports | 22 (SSH), 443 (HTTPS) |

PostgreSQL, the bot and Caddy all run on this one box. The app listens on
`127.0.0.1:8080` and is never reachable from outside.

## Traffic

```
browser → 187.13.70.46:443 → Caddy (TLS)
                                    → 127.0.0.1:8080 → app
                                      └─ injects X-Platform-Auth
                                      └─ app checks the session cookie
```

Two independent gates, with different jobs.

`X-Platform-Auth` is the transport secret. It is attached by Caddy and proves
the request came through the proxy rather than straight at port 8080. Without
it, anything that could reach the port could issue commands.

The session cookie is the account. It is issued when a user signs in at
`/login` and identifies *who* is asking, which is what separates one user's
notes from another's. Without it, the edge may be reachable but there is no one
to act as.

Neither is enough on its own, and neither replaces the other: the transport
secret says *where the request came from*, the cookie says *who sent it*.

## Install

```bash
apt update && apt install -y postgresql-16 caddy git

useradd --system --shell /usr/sbin/nologin automation
mkdir -p /opt/automation-platform
git config --global --add safe.directory /opt/automation-platform
```

Create the database interactively so the password never lands in shell
history:

```bash
sudo -u postgres psql
```

```sql
CREATE USER automation WITH ENCRYPTED PASSWORD '<same value as DB_PASSWORD>';
CREATE DATABASE automation_platform OWNER automation;
\q
```

Confirm the app's path works before going further:

```bash
read -s -p "DB_PASSWORD: " PGPASSWORD; echo
PGPASSWORD="$PGPASSWORD" psql -h 127.0.0.1 -U automation -d automation_platform -c 'select 1;'
```

Then, and only then, sync dependencies:

```bash
chown -R automation:automation /opt/automation-platform
uv sync --frozen --no-dev
chown -R automation:automation .venv
```

Order matters. Syncing before `chown` produces a root-owned `.venv` the service
cannot read.

## Configure

`/opt/automation-platform/.env`, mode `600`, owner `automation`:

```
BOT_TOKEN=...
DB_HOST=localhost
DB_PORT=5432
DB_NAME=automation_platform
DB_USER=automation
DB_PASSWORD=...

WEB_HOST=127.0.0.1
WEB_PORT=8080
WEB_ACCESS_TOKEN=<openssl rand -hex 32>
SESSION_TTL_DAYS=30
SESSION_COOKIE_SECURE=true
SIGNUP_INVITE_CODE=   # empty: no sign-up page at all (see Accounts)
PROXY_URL=          # empty: api.telegram.org is reachable from Germany
```

`app/core/config.py` calls `load_dotenv()` itself. Do not "simplify" that away
— see Gotchas.

### Accounts

Two ways in, and neither of them is an open door.

**Sign-up page.** Enable it with an invite code in `.env`:

```bash
SIGNUP_INVITE_CODE=$(openssl rand -hex 16)
```

Leave the variable unset and there is no sign-up page at all: the login form
offers no link and `/signup` answers 404. That is the default for every
deployment that never thought about it, so forgetting the variable fails
closed rather than open.

With a code set, `/signup` asks for the code, a username and a password, and
a successful registration signs the person in on the spot. The code is a gate
rather than a credential: it decides who may join, it is never stored, and
rotating it does not disturb the accounts that already exist. There is no
approval step behind it — anyone holding the code can create an account —
which is why it should be long. Guessing it is the only thing standing
between the internet and a working login.

**From the host.** No browser involved, which is what to reach for when the
invite code should never be typed anywhere:

```bash
cd /opt/automation-platform
python -m app.manage create-user alexey     # prompts, never takes argv
python -m app.manage list-users
```

The password is read with `getpass` and is never accepted as a command line
argument — arguments are visible in `ps` and land in shell history. On a box
without a terminal, pipe it in instead: `echo "<password>" | python -m
app.manage create-user alexey`.

The command applies pending migrations first, so it works on a fresh install
before the service has ever started.

`SESSION_COOKIE_SECURE` must stay `true` in production. It cannot be inferred
from the request: the app talks to Caddy over plain HTTP on loopback, so the
socket is never TLS and `request.secure` is false even behind HTTPS. Set it to
`false` only for local `http://` development.

## systemd

`/etc/systemd/system/automation-platform.service`:

```ini
[Unit]
Description=Automation Platform
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=automation
WorkingDirectory=/opt/automation-platform
ExecStart=/opt/automation-platform/.venv/bin/python -m app.main
Restart=always
RestartSec=10
RestartPreventExitStatus=78

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory` is required: `.env` and `sql/migrations/` resolve relative
to it.

Exit code `78` is `EX_CONFIG` — missing `BOT_TOKEN` only. systemd must not
retry that one, because retrying a config error cannot help; it retries
everything else, so a transient PostgreSQL outage recovers on its own.

```bash
systemctl daemon-reload && systemctl enable --now automation-platform
```

## Caddy

Version 2.6.2 comes from the Ubuntu universe repository.

Caddy holds no credentials any more — it used to run `basicauth`, and that has
been removed. All authentication is the application's job now, which is what
makes per-user accounts and per-user data possible. Caddy's only remaining
duties are TLS and attaching the transport secret.

`/etc/caddy/Caddyfile`:

```
automationplatformlebedev.ru, automationplatformlebedev.online {
	reverse_proxy 127.0.0.1:8080 {
		header_up X-Platform-Auth "<WEB_ACCESS_TOKEN value>"
	}
}
```

The header value must byte-match `WEB_ACCESS_TOKEN` in `.env` — edit both when
rotating. Anyone who knows the domain can now reach the login page; the page
itself still requires the header, so a request that bypasses Caddy never gets
as far as a password prompt.

Validate before reloading; a bad config should never reach the running
service:

```bash
caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
```

## Certificates

Caddy owns this. It issues Let's Encrypt on first successful HTTP-01 challenge
and renews automatically. Nothing to configure, nothing to monitor.

The free **DomainSSL** offered with the `.ru` domain is not needed. Installing
it means manual renewal once a year, and Caddy will not touch a certificate it
did not obtain. Related: the `_globalsign-domain-verification` TXT record that
was added is **not** present in the authoritative zone, so that certificate
cannot currently be issued at all.

## DNS

Delegation sits with Ruvds, and Ruvds serves both zones.

```
ns1.reg.ru  → REFUSED for automationplatformlebedev.ru (no longer authoritative)
NS          → ns1.ruvds.com / ns2.ruvds.com
A @         → 187.13.70.46
```

Editing records in the reg.ru panel does nothing for either domain. All zone
edits go through the Ruvds panel.

### Known inconsistency: UDP vs DoH

| Transport | Answer |
|---|---|
| UDP to any resolver | `198.18.0.x` (parking placeholder) |
| TCP | `187.13.70.46` |
| DNS-over-HTTPS | `187.13.70.46` |

`198.18.0.0/15` is the RFC 2544 benchmark range — it is not routable, so
anything resolving over UDP reaches nothing. Symptoms: `ERR_SSL_PROTOCOL_ERROR`
behind a corporate proxy, `ERR_NAME_NOT_RESOLVED`, "site did not send data".

Commands to re-check:

```bash
dig +short automationplatformlebedev.ru A @ns1.ruvds.com   # authoritative
dig +short automationplatformlebedev.ru A @1.1.1.1         # public UDP
curl -s 'https://dns.google/resolve?name=automationplatformlebedev.ru&type=A'
```

Wait for all three to agree on `187.13.70.46`. This is a provider-side issue;
do not re-create records while it is open, as re-saving appears to restart
their migration.

## Verify

```bash
curl -sI https://automationplatformlebedev.ru/            # 303, Location: /login
curl -si localhost:8080/health                            # 401: no proxy header
curl -si -H "X-Platform-Auth: <WEB_ACCESS_TOKEN value>" localhost:8080/health
systemctl status automation-platform caddy
journalctl -u automation-platform -n 50
```

Expected shape of each:

| Call | Result |
|---|---|
| `https://…/` through Caddy | `303` to `/login`, `Server: Caddy` |
| `/health` straight on 8080 | `401` — transport secret missing |
| `/health` with the header, no cookie | `200` — health is deliberately not behind a session |
| `/api/command` with the header, no cookie | `401` |
| `/signup` with the header, no cookie | `404` while `SIGNUP_INVITE_CODE` is unset, `200` once it is |

A fresh database logs `Applied 4 new migration(s)` on first start. An existing
one that already had `001`–`003` logs `Applied 1 new migration(s)`, for
`004_users_sessions.sql`.

Then open the site in a browser: it must land on the sign-in form, and a wrong
password must return `Incorrect username or password.` without saying which
half was wrong. The login page shows a sign-up link only when
`SIGNUP_INVITE_CODE` is set in `.env`; following it and registering should
leave you on the index, already signed in, with a new row in `users`.

## Update

```bash
cd /opt/automation-platform
git pull && uv sync --frozen --no-dev
chown -R automation:automation .
systemctl restart automation-platform
journalctl -u automation-platform -n 50
```

`chown` after pull: the repo is owned by `automation`, but the deploy is run
as root, so new files come out root-owned.

## Access from a corporate laptop

The work laptop uses a managed browser where Secure DNS cannot be changed, and
the proxy performs TLS inspection. Two things are needed, neither of them on
this server:

1. DNS above must agree on `187.13.70.46`, otherwise the proxy cannot reach
   the VPS at all.
2. IT must add `automationplatformlebedev.ru` to the allowlist for HTTPS
   inspection and corporate DNS resolution.

An SSH tunnel was considered as the fallback (`127.0.0.1:8080` on the server
forwarded to a local port), and it does not work as-is: a browser cannot send
`X-Platform-Auth`, so it would also need a second Caddy listener on
`127.0.0.1:8081` that injects the header. **Dropped** — a corporate MTProto
gateway was issued instead, and the bot needs no tunnel, no allowlist and no
second listener. Recorded in DECISIONS.md.

## Database backup

Daily, run by systemd rather than by the application. The job must not depend
on the platform being able to start: the one time a backup is desperately
needed is when the application is broken.

`/etc/systemd/system/automation-platform-backup.service`:

```ini
[Unit]
Description=Automation Platform database backup
After=postgresql.service

[Service]
Type=oneshot
User=automation
WorkingDirectory=/opt/automation-platform
ExecStart=/opt/automation-platform/.venv/bin/python -m app.manage backup
```

`/etc/systemd/system/automation-platform-backup.timer`:

```ini
[Unit]
Description=Daily Automation Platform database backup

[Timer]
OnCalendar=*-*-* 04:17:00
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` runs a missed backup at the next boot. That matters more
than the hour itself: nothing guarantees the box is up at 04:17.

Point `BACKUP_DIR` in `.env` outside the checkout and create the directory
before the first run — the job runs as `automation`:

```bash
mkdir -p /var/backups/automation-platform
chown automation:automation /var/backups/automation-platform
chmod 700 /var/backups/automation-platform
```

Files are `scheduled_YYYY-MM-DD_HH-MM-SS.sql`, mode `0600`, because the dump
contains the scrypt password hashes. `BACKUP_RETENTION_DAYS` (default 14) is
how long they are kept; `0` keeps everything.

```bash
systemctl daemon-reload && systemctl enable --now automation-platform-backup.timer
systemctl start automation-platform-backup.service
journalctl -u automation-platform-backup.service -n 20 --no-pager
systemctl list-timers automation-platform-backup.timer
```

### Restore

Verified against a scratch database: tables, ownership, recorded migrations
and sequences all came back, and an `INSERT` worked immediately afterwards.

The dump is `0600` and owned by `automation`, so read it as root and let the
stream reach `postgres`:

```bash
systemctl stop automation-platform
sudo -u postgres dropdb --if-exists automation_platform
sudo -u postgres createdb -O automation automation_platform
sudo -u postgres psql -q -v ON_ERROR_STOP=1 -d automation_platform \
  < /var/backups/automation-platform/scheduled_<timestamp>.sql
systemctl start automation-platform
```

The dump carries `schema_migrations`, so the application starts without
reapplying anything, and `pg_dump` emits the `setval` calls that leave the
sequences usable.

## Gotchas

Things that cost real debugging time. Read before editing.

- **`load_dotenv()` in `config.py`.** `Config` reads `os.getenv` at import
  time. An entry point that loads `.env` after importing app modules silently
  gets defaults — on the server this meant an empty `DB_PASSWORD` and a
  crash-looping service that looked like a database problem. It has a
  subprocess regression test in `tests/test_config.py`.
- **`su automation` fails.** The user has `nologin`. Work as root and `chown`
  afterwards, or use `sudo -u`.
- **git: "dubious ownership".** Root reading an `automation`-owned repo. Fix
  with `git config --global --add safe.directory`, then `chown` back.
- **Test env vars must be set before the first app import.** `Config` reads
  `os.getenv` into class attributes once, when its class body runs. Setting
  `DB_NAME` inside a pytest fixture happens *after* every test module has
  already imported `app.core.config`, so the values are ignored and the tests
  run against the development database — emptying it. `tests/conftest.py`
  sets them at module level, above the app imports, and carries a
  `# ruff: noqa: E402` for exactly that reason. Do not move them into a
  fixture.
- **`_clean_db` drops every table, not a list.** It used to name the tables
  it knew about, so a table created by a newer migration survived, and the
  next migration run failed on `relation ... already exists`. Listing tables
  in a test helper is a maintenance bug waiting to happen.
- **`/health` is not behind a session.** It has to answer while the database
  is down; requiring a session would make it fail at authentication instead of
  reporting. It still needs `X-Platform-Auth`, and it reveals only a boolean.
- **Panel ≠ authoritative zone.** Check who actually serves the zone before
  trusting what a registrar's panel displays.

## Local checks

```bash
uv run ruff check .
uv run pytest
```

182 tests. Run before pushing anything that touches config, auth or
migrations. The parts worth knowing about:

- `tests/test_config.py` — subprocess test for the `.env` import-order bug;
  it fails if that fix is reverted, because a module-level ordering problem
  is invisible to in-process tests.
- `tests/test_auth.py` — both layers separately: transport secret and session;
  then the sign-up gate, a failed attempt leaving no account behind, and that
  sign-up is off by default when no invite code is configured.
- `tests/test_accounts.py` — password storage, session lifetime, and that two
  web users cannot read each other's notes.
- `tests/test_passwords.py` — a corrupt or truncated hash fails the login
  instead of raising.
