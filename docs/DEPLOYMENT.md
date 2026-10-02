# Deployment

Single VPS runs the bot, PostgreSQL and the web interface. One systemd unit,
one event loop.

## Requirements

| | |
|---|---|
| vCPU / RAM / Disk | 1 / 1 GB / 10 GB SSD |
| OS | Ubuntu 24.04 LTS |
| Runtime | Python 3.12 via `uv` |
| Ports | 443 (HTTPS), 22 (SSH) |

The workload is a 30-second poll and small queries. Do not pay for more.

## Web access

The web interface has **no user accounts**. It is protected by a secret header
that only the reverse proxy knows, plus whatever the proxy requires to let a
request through. Both layers are required — neither alone is sufficient.

```
browser → 443 → Caddy (TLS, proxy auth) → 127.0.0.1:8080 → app
                                          └─ injects X-Platform-Auth
```

The app binds to loopback. Port 8080 is never exposed.

## Install

```bash
sudo apt update && sudo apt install -y git postgresql-client
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc

sudo mkdir -p /opt/automation-platform && sudo chown $USER:$USER /opt/automation-platform
cd /opt/automation-platform && git clone git@github.com:Dorian101/-automation-platform.git .
uv sync --frozen --no-dev
```

## Configure

```bash
cp .env.example .env && nano .env
```

Required:

```
BOT_TOKEN=...
DB_HOST=localhost
DB_NAME=automation_platform
DB_USER=automation
DB_PASSWORD=...

WEB_HOST=127.0.0.1
WEB_PORT=8080
WEB_ACCESS_TOKEN=<openssl rand -hex 32>
PROXY_URL=          # only if api.telegram.org is unreachable from the VPS
```

`WEB_ACCESS_TOKEN` must equal the secret Caddy injects. Empty disables the
check, which must not happen in production.

```bash
chmod 600 .env
```

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
RestartPreventExitStatus=1

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory` is required: `.env` and `sql/migrations/` are resolved
relative to it.

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now automation-platform
```

## Reverse proxy

Needs a DNS record to the VPS. `/etc/caddy/Caddyfile`:

```
automation.example.com {
    basic_auth {
        alexey $2a$14$...
    }
    reverse_proxy 127.0.0.1:8080 {
        header_up X-Platform-Auth "<WEB_ACCESS_TOKEN value>"
    }
}
```

Hash the password with `caddy hash-password`, never paste a plaintext one.

```bash
sudo systemctl reload caddy
```

Open 443 only. 8080 stays on loopback.

## Verify

```bash
curl -sI https://automation.example.com/          # 401 without proxy credentials
curl -s localhost:8080/health                     # 401 locally: proxy only
sudo journalctl -u automation-platform -f
```

Expected on first start: `Applied 2 new migration(s)` — indexes and the
`user_id` identity migration run automatically.

## Update

```bash
cd /opt/automation-platform
git pull && uv sync --frozen --no-dev
sudo systemctl restart automation-platform
sudo journalctl -u automation-platform -n 50
```

## Database

```bash
sudo -u postgres pg_dump -d automation_platform -f /var/backups/automation_$(date +%F).sql
```

Take one before any deploy that includes a new migration. `BackupManager`
exists but is not wired into startup.

## Local checks

```bash
uv run ruff check app/ tests/
uv run pytest tests/
```