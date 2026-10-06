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
browser → 187.13.70.46:443 → Caddy (TLS, basicauth)
                                     → 127.0.0.1:8080 → app
                                       └─ injects X-Platform-Auth
```

Two independent gates. `basicauth` is the human-facing credential at the edge.
`X-Platform-Auth` proves the request came through Caddy and not around it.
Neither alone is enough: without the secret, anything that could reach 8080
gets in; without the password, the edge is open.

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
PROXY_URL=          # empty: api.telegram.org is reachable from Germany
```

`app/core/config.py` calls `load_dotenv()` itself. Do not "simplify" that away
— see Gotchas.

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

Version 2.6.2 comes from the Ubuntu universe repository. The directive is
**`basicauth`**, not `basic_auth` — the latter needs Caddy 2.8+ and will be
rejected.

`/etc/caddy/Caddyfile`:

```
automationplatformlebedev.ru, automationplatformlebedev.online {
	basicauth {
		alexey $2a$14$...
	}
	reverse_proxy 127.0.0.1:8080 {
		header_up X-Platform-Auth "<WEB_ACCESS_TOKEN value>"
	}
}
```

Generate the hash with `caddy hash-password` (promotes input invisibly, never
paste a plaintext password). The header value must byte-match
`WEB_ACCESS_TOKEN` in `.env` — edit both when rotating.

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
curl -I https://automationplatformlebedev.ru/            # 401 + Server: Caddy
curl -i localhost:8080/health                            # 401: proxy-only
systemctl status automation-platform caddy
journalctl -u automation-platform -n 50
```

A fresh database logs `Applied 3 new migration(s)` on first start.

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

While that is pending, the fallback is an SSH tunnel in Termius
(`127.0.0.1:8080` on the server → a local port). The browser cannot send
`X-Platform-Auth`, so this needs a second Caddy listener on `127.0.0.1:8081`
that injects the header. **Not implemented yet.**

## Database backup

Manual only, nothing scheduled:

```bash
sudo -u postgres pg_dump -d automation_platform -f /var/backups/automation_$(date +%F).sql
```

**Open item:** no automatic backups exist. The only copy of every note and
reminder lives in this database. Before any deploy containing a migration,
take a dump.

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
- **Caddy directive name.** `basicauth` on 2.6.2, `basic_auth` on 2.8+.
- **Panel ≠ authoritative zone.** Check who actually serves the zone before
  trusting what a registrar's panel displays.

## Local checks

```bash
uv run ruff check app/ tests/
uv run pytest tests/
```

97 tests, including the subprocess dotenv test. Run before pushing anything
that touches config, auth or migrations.
