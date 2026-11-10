# Deployment

A runbook for putting this system on a box of your own, and for restoring it
on a new one. Nothing here names a host, a domain or an address: the values
that change per deployment are written `<angle brackets>` and are yours to
fill in. The versions and the shape of the thing are not — those are what
were actually run, and a runbook that hedges them is not one anybody can
follow.

Read "Gotchas" before changing anything.

## Inventory

Fill this in first. Everything below refers back to it.

| | |
|---|---|
| Hostname | `<host>` |
| Public IP | `<ip>` |
| OS | Ubuntu 24.04 |
| PostgreSQL | 16.15 |
| Caddy | 2.11.7, from the official repository, not Ubuntu's |
| Checkout | `/opt/automation-platform`, owned by `automation` |
| Domain | `<domain>` |
| Nameservers | `<ns1>`, `<ns2>` |
| Exposed ports | 22 (SSH), 80 (HTTPS redirect and the ACME challenge), 443 (HTTPS, TCP and UDP for HTTP/3) |

PostgreSQL, the bot and Caddy all run on this one box. The app listens on
`127.0.0.1:8080` and is never reachable from outside.

## Traffic

```
browser → <ip>:443 → Caddy (TLS)
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

## Host hardening

The box faced the internet with root password login on, no firewall and no
ban on guessing: the SSH journal held about fourteen thousand failed logins a
day. Three changes, all made by hand as root.

**Keys only.** `/etc/ssh/sshd_config.d/00-hardening.conf`:

```
PermitRootLogin prohibit-password
PasswordAuthentication no
```

The `00-` matters: sshd keeps the first value it reads, and the image ships
`50-cloud-init.conf` with `PasswordAuthentication yes`. Check that a key login
works before reloading, and keep the old session open until a new one does:

```bash
ssh -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@<ip> 'echo KEY_OK'
sshd -t && systemctl reload ssh
sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication) '
```

The provider's web console is not SSH and still takes the root password, which
is the way back from a lost key.

**Firewall.** The image has no ufw; install it and allow SSH first:

```bash
apt-get install -y ufw
ufw default deny incoming && ufw default allow outgoing
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
ufw --force enable
```

PostgreSQL, the app and Caddy's admin API listen on loopback anyway; the
firewall is the second line for anything that one day binds to every address.

**fail2ban** on sshd: five failures in ten minutes ban the address for an hour.
`/etc/fail2ban/jail.local`:

```ini
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
```

`fail2ban-client status sshd` shows the current bans.

### Morning SSH digest

Every morning at 09:00 Moscow time the administrators get a Telegram message:
refused SSH connections over the last day and from how many addresses, the
usernames tried most, fail2ban's bans, and every successful login with its
user, address and method.

The collector reads the journal and asks fail2ban, so it runs as root. It is
**copied out of the checkout, not run from it**: `automation` owns the
checkout, and a root job executing a file that user can rewrite would give
root to anything that compromises the application. The sending is done as
`automation` through `app.manage notify-admins`.

```bash
install -o root -g root -m 0755 /opt/automation-platform/scripts/ssh-digest.sh \
  /usr/local/sbin/automation-platform-ssh-digest
/usr/local/sbin/automation-platform-ssh-digest --print
```

`--print` shows the message without sending it. After a change to the script
in the repository, read it, then run the `install` line again.

`/etc/systemd/system/automation-platform-ssh-digest.service`:

```ini
[Unit]
Description=Automation Platform morning SSH digest
After=network-online.target fail2ban.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/automation-platform-ssh-digest
PrivateTmp=true
```

`/etc/systemd/system/automation-platform-ssh-digest.timer`:

```ini
[Unit]
Description=Daily SSH digest to the administrators

[Timer]
OnCalendar=*-*-* 09:00 Europe/Moscow
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload && systemctl enable --now automation-platform-ssh-digest.timer
systemctl start automation-platform-ssh-digest.service
journalctl -u automation-platform-ssh-digest.service -n 5 --no-pager
```

### Security updates

Ubuntu's `unattended-upgrades` installs security updates once a day, PostgreSQL
and OpenSSH included. Four additions, all as root:

1. **Livepatch** fixes the running kernel without a reboot. The Pro client is
   not in the provider's image.

   ```bash
   apt-get update && apt-get install -y ubuntu-pro-client
   pro attach <token>
   pro enable livepatch
   pro status
   canonical-livepatch status --verbose
   ```

   `pro status` must show `livepatch enabled`; `canonical-livepatch status`
   must name the running kernel as supported.

2. **A reboot when one is needed**, at 04:00 Moscow time (the host runs in
   UTC). It happens only on nights that leave `/var/run/reboot-required`;
   the app, Caddy, PostgreSQL and fail2ban are enabled and come back by
   themselves.

   ```bash
   cat > /etc/apt/apt.conf.d/52automation-reboot <<'CONF'
   Unattended-Upgrade::Automatic-Reboot "true";
   Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
   Unattended-Upgrade::Automatic-Reboot-Time "01:00";
   CONF
   unattended-upgrade --dry-run --debug 2>&1 | grep -i "reboot"
   ```

3. **Caddy** comes from its own repository, which `unattended-upgrades` skips
   unless told otherwise.

   ```bash
   cat > /etc/apt/apt.conf.d/53automation-caddy <<'CONF'
   Unattended-Upgrade::Origins-Pattern {
           "origin=cloudsmith/caddy/stable,archive=any-version";
   };
   CONF
   unattended-upgrade --dry-run --debug 2>&1 | grep -i "caddy"
   apt-get install -y -o Dpkg::Options::="--force-confold" caddy
   caddy version && systemctl is-active caddy
   ```

4. **A message when something is wrong**, and silence otherwise.
   `scripts/update-check.sh` looks at the last automatic run, its log for the
   day, security updates still pending, a reboot waiting over a day and
   Livepatch, and writes to the administrators only if one of them is off.
   Root runs it, so it is copied out of the checkout like the SSH digest.

   ```bash
   install -o root -g root -m 0755 /opt/automation-platform/scripts/update-check.sh \
     /usr/local/sbin/automation-platform-update-check
   /usr/local/sbin/automation-platform-update-check --print
   ```

   `/etc/systemd/system/automation-platform-update-check.service`:

   ```ini
   [Unit]
   Description=Automation Platform security update check

   [Service]
   Type=oneshot
   ExecStart=/usr/local/sbin/automation-platform-update-check
   ```

   `/etc/systemd/system/automation-platform-update-check.timer`:

   ```ini
   [Unit]
   Description=Automation Platform security update check, daily

   [Timer]
   OnCalendar=*-*-* 09:00 UTC
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

   ```bash
   systemctl daemon-reload && systemctl enable --now automation-platform-update-check.timer
   systemctl list-timers automation-platform-update-check.timer --no-pager
   ```

Python dependencies are audited every day at 04:00 UTC by
`.github/workflows/audit.yml` (GitHub mails the owner when it fails), and
Dependabot security updates open a pull request as soon as an advisory is
published.

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
CSRF_SECRET=<openssl rand -hex 32>
PUBLIC_BASE_URL=https://<domain>
SESSION_TTL_DAYS=30
SESSION_COOKIE_SECURE=true
SIGNUP_OPEN=false     # true: open registration; anything else: no sign-up page
PROXY_URL=          # empty: api.telegram.org is reachable from Germany

TELEGRAM_BOT_USERNAME=   # empty: no Telegram link button (see Telegram linking)
TELEGRAM_LOGIN_MAX_AGE_SECONDS=300   # how long a Login Widget signature stays fresh

VAPID_PRIVATE_KEY=   # web push; all three from: python -m app.manage vapid-keys
VAPID_PUBLIC_KEY=    # empty: no push, notifications go to Telegram as before
VAPID_SUBJECT=mailto:<owner address>   # push services contact on abuse

# How the administration pages print timestamps. Rows are stored in UTC.
DISPLAY_TIMEZONE=UTC

# Rate limits on the public forms (per process; the defaults are shown).
# 0 disables a limit, which is only sensible behind a limiting edge.
RATE_LIMIT_WINDOW_SECONDS=60
LOGIN_ATTEMPTS_PER_WINDOW=5
LOGIN_ATTEMPTS_PER_ADDRESS_PER_WINDOW=20
SIGNUP_ATTEMPTS_PER_WINDOW=10
SIGNUP_WINDOW_SECONDS=3600
DEMO_ATTEMPTS_PER_WINDOW=200
DEMO_WINDOW_SECONDS=3600

# Keeping your own traffic out of the visit log, and the secret that does
# it from anywhere (both are described below).
VISIT_IGNORE_IPS=
VISIT_PROBE_TOKEN=

# Where the daily database dump goes. Outside the checkout, and it must
# exist and belong to the user the job runs as.
BACKUP_DIR=/var/backups/automation-platform
BACKUP_RETENTION_DAYS=14
# Seals the copy of each scheduled backup sent to the administrators'
# Telegram chats. Empty keeps every backup on this host only.
BACKUP_PASSPHRASE=
```

`app/core/config.py` calls `load_dotenv()` itself. Do not "simplify" that away
— see Gotchas. `.env.example` carries the same list with the defaults and a
comment on each; this block is the subset a deployment cannot do without.

### Timestamps in the administration pages

The visit log is written by `CURRENT_TIMESTAMP` on the host, which keeps UTC, and
the column carries no offset. The stored value is therefore UTC on every
deployment, and `prune-visits` compares it against a `datetime.now()` from the
same host, which is why that pairing stays correct.

Set `DISPLAY_TIMEZONE` to an IANA name to read it on your own clock:

```
DISPLAY_TIMEZONE=Europe/Moscow
```

It changes only what the `/admin/visits` page prints, and the column heading
names the zone it printed in. Rows written before the change need no migration —
they were UTC all along and now simply read correctly.

### Keeping your own traffic out of the visit log

The log records pages looked at and nothing about who looked: no address is
stored. That leaves the owner's own checking sitting in the same table as a
stranger's, indistinguishable, and the counts — including the growth rate that
decides when `prune-visits` becomes worth installing — counting both.

Set `VISIT_IGNORE_IPS` to a comma-separated list of addresses and requests from
them leave no row at all. Your browser, a tool running on your machine, and your
curl all come from one of them, so one setting covers all three. Your sign-ins
stop being recorded too, which is the point: they were never the interesting
part of the log.

```
VISIT_IGNORE_IPS=203.0.113.9,198.51.100.4
```

The address is compared against the proxy's `X-Forwarded-For` and then dropped.
Nothing is added to any table, which is why this is a filter on the way in
rather than a column: a column holding the address would answer the question
the filter exists to answer, and would undo the decision not to store them.

This works because Caddy **replaces** `X-Forwarded-For` instead of passing the
caller's through, which was checked against the running deployment rather than
assumed. Adding `trusted_proxies` to the `reverse_proxy` block would make the
proxy trust an incoming value, and a caller could then list themselves among the
excluded addresses and keep their own rows out of your log. Nothing else about
the setting depends on it.

Two things worth knowing before you fill it in:

- **Put it in the server's `.env`, not in the repository.** `.env.example`
  documents the setting with example addresses; the real ones belong on the
  machine they describe.
- **A residential address usually rotates.** If the rule stops working, this
  is why: the log is counting you again, and the entry needs updating. Proxies
  and fixed addresses do not have this problem.

The `hide=1` box on the page stays useful for the other case: a session of
yours recognised as the administrator's from an address that is not on this
list, such as a phone on a mobile network.

### Measuring the site from somewhere else

The address list has one blind spot, and it is a large one: it only knows the
machines named in it. Work done against the site from anywhere else — a
measurement from a laptop on a different network, a request through a proxy —
arrives from an address that is on no list, and every one of them lands in the
log as somebody visiting. That is not hypothetical here: measuring the site
once put a hundred and thirty rows into the table in an afternoon.

`VISIT_PROBE_TOKEN` is the other half. A request carrying it in the
`X-Platform-Probe` header leaves no row, from whatever address it came:

```
VISIT_PROBE_TOKEN=<openssl rand -hex 32>
```

```
curl -H "X-Platform-Probe: $VISIT_PROBE_TOKEN" https://<domain>/about
```

Both are compared on the way in and neither is stored, and the token covers
sign-ins as well as page views. Caddy passes the header through without being
told to, so nothing has to change in the Caddyfile — verified by asking for a
page with it and watching the log stay empty, not by reading the config.

Two things to know before you set it:

- **It is a shared secret, not a credential.** Anyone holding it can keep
  themselves out of the log, and a header is no secret at all against someone
  who wants to send one. It keeps your own working out of a count meant to
  answer one question — was anybody here. It is not a way to refuse a visitor,
  and nothing depends on it being robust.
- **Unset means nothing is trusted.** `compare_digest` calls two empty strings
  equal, so an empty token would otherwise trust every request on the site. If
  you never probe, leave it empty and the log records everything as before.

Keeping both is the point: the address covers your browser, the token covers
everything you do deliberately, and neither is a column on the table.

### Telegram linking

Optional. With `TELEGRAM_BOT_USERNAME` empty the console shows no link button
and `/account/telegram/link` answers 404 — nothing to configure, nothing to
undo.

To turn it on, two steps, and the first is not done from here:

1. In `@BotFather`, `/setdomain`, pointing at the domain the console is served
   on. Telegram checks this before showing its login button, so nothing works
   until it is set, and it cannot be tested on localhost.
2. Set `TELEGRAM_BOT_USERNAME` to the bot's username without the leading `@`
   and restart the service.

Accounts can also be paired from the host, which needs neither:

```
python -m app.manage link-telegram <username> <chat_id>
python -m app.manage unlink-telegram <username>
python -m app.manage list-links
```

The chat id is what `getUpdates` reports, or the numeric part of the bot's URL
when you open `t.me/<bot>`. `manage` is also the way out if a link is wrong
and the browser cannot reach Telegram to fix it.

Linking changes where reminders are delivered. It does not merge the two
identities: web data stays under `web:<name>` and Telegram data stays under
`telegram:<id>`.

### Accounts

Two ways in. Registration is the open one, and it is off by default.

**Sign-up page.** Enable it with the flag in `.env`:

```bash
SIGNUP_OPEN=true
```

Leave it false (or unset) and there is no sign-up page at all: the login form
offers no link and `/signup` answers 404. That is the default for every
deployment that never thought about it, so forgetting the flag fails closed
rather than open.

With `SIGNUP_OPEN=true`, `/signup` asks for a username and a password and a
successful registration signs the person in on the spot. There is no approval
step and no invite code behind it — anyone who reaches the form can create an
account — which is exactly the trade the flag makes. The per-address rate limit
(see below) is the only brake, so a deployment that opens sign-up should expect
it to be used by strangers.

**From the host.** No browser involved, which is what to reach for when the
owner does not want anything typed through a web form:

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

**Administration from the host.** When a credential has leaked, these run from
the machine with no browser involved:

```bash
python -m app.manage disable-user <username>    # stop sign-in, revoke sessions
python -m app.manage enable-user <username>     # allow sign-in again
python -m app.manage reset-password <username>  # new password, revoke sessions
python -m app.manage revoke-sessions <username> # end sessions, keep password
python -m app.manage reset-passkeys <username>  # remove passkeys, revoke sessions
```

`reset-passkeys` is the way back for an account whose only passkey device is
lost. Passkeys need `PUBLIC_BASE_URL`: its host is the relying party, so it
must be the domain the browser shows. Without it nobody holds admin rights.

`reset-password` reads the password with `getpass`, like `create-user`, and can
be piped the same way. `disable-user` and `reset-password` revoke every live
session of the account, so the holder of a stolen cookie is signed out at the
same time. None of the commands delete data — a disabled account keeps its
notes until it is enabled again. There is no rename.

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

### Sandboxing

The service runs with its privileges taken away, which is what decides how far
a compromise of the process reaches. Applied as a drop-in, so the unit above
stays the plain one:

`/etc/systemd/system/automation-platform.service.d/override.conf`

```ini
[Service]
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/automation-platform /var/backups/automation-platform
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictNamespaces=true
LockPersonality=true
```

Write the file rather than using `systemctl edit`: that opens `$EDITOR`, and a
host without one fails to save while reporting nothing.

`ProtectSystem=strict` makes the whole hierarchy read-only apart from the two
paths named, which is why they are named. Bytecode caching is not affected —
Python skips writing it rather than failing — and the application itself writes
nothing outside the database.

The backup and the two cleanup units carry the same directives in their own
`override.conf` under `/etc/systemd/system/automation-platform-<unit>.service.d/`,
with one difference: the backup gets `ReadWritePaths=/var/backups/automation-platform`
and the cleanups get none, because they only delete rows. Each was started by
hand after the change and finished with `Result=success`.

To check what systemd actually applied, rather than what the file says:

```bash
systemctl show automation-platform -p NoNewPrivileges -p ProtectSystem -p ReadWritePaths
```

## Caddy

Install from the official repository. Ubuntu's universe package is a minor
release behind and carries advisories, so the distribution copy is not the one
to run. The repository's suite is called `any-version` and its path is
`deb/debian`, **not** `debian/ubuntu` — a line built from the Ubuntu codename
returns 404 and the repository silently does not attach.

```bash
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update
```

`apt-cache policy caddy` should then name a candidate above the installed
version. When installing, keep the local `Caddyfile`:

```bash
apt-get install -y -o Dpkg::Options::="--force-confold" caddy
```

To go back to the distribution package, remove the list file, refresh, and
install the recorded version explicitly with `--allow-downgrades`.

Caddy holds no credentials any more — it used to run `basicauth`, and that has
been removed. All authentication is the application's job now, which is what
makes per-user accounts and per-user data possible. Caddy's only remaining
duties are TLS and attaching the transport secret.

`/etc/caddy/Caddyfile`:

```
<domain> {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8080 {
		header_up X-Platform-Auth "<WEB_ACCESS_TOKEN value>"
	}
}
```

`encode` compresses the text responses (HTML, scripts, CSS) for clients that
ask for it: `/about` goes from about 63 KB to 20 KB. Fonts are already
compressed and pass through as they are.

One block per domain you serve. The name `Caddy` issues a certificate for has
to be the one a reader types, which is the whole reason the domain is written
here rather than anywhere the app could guess.

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

Caddy owns this. It issues Let's Encrypt on the first successful HTTP-01
challenge and renews automatically. Nothing to configure, nothing to monitor.

Do not also install a free certificate from whoever sells you the domain: it
means manual renewal every year, and Caddy will not touch a certificate it did
not obtain.

## DNS

An `A` record for `<domain>` pointing at `<ip>`, delegated to `<ns1>` and
`<ns2>`. Edit the records wherever the zone is actually served — a registrar's
panel is not always the same thing as the nameserver, and records saved in the
wrong place look correct and do nothing.

Before anything else works, all three of these have to agree:

```bash
dig +short <domain> A @<ns1>            # authoritative
dig +short <domain> A @1.1.1.1          # public UDP
curl -s 'https://dns.google/resolve?name=<domain>&type=A'
```

**A `198.18.x.x` answer is not your server.** That is `198.18.0.0/15`, the
RFC 2544 benchmark range — reserved, not routable, and what several registrars
park an unconfigured domain with. A resolver answering over UDP will hand it
back happily, so the browser goes nowhere. The symptoms are not recognisable
as DNS: `ERR_SSL_PROTOCOL_ERROR` behind a proxy, `ERR_NAME_NOT_RESOLVED`,
"site did not send data". TCP and DNS-over-HTTPS often disagree with plain
UDP here, which is the clue — check all three and see whether they differ.

Do not re-create records while it is open: a save often restarts the
provider's own migration and makes the answer lag further behind.

## Verify

```bash
curl -sI https://<domain>/            # 303, Location: /login
curl -si localhost:8080/health                            # 401: no proxy header
curl -si -H "X-Platform-Auth: <WEB_ACCESS_TOKEN value>" localhost:8080/health
curl -si -H "X-Platform-Auth: <WEB_ACCESS_TOKEN value>" localhost:8080/about   # 200: public document
systemctl status automation-platform caddy
journalctl -u automation-platform -n 50
```

Expected shape of each:

| Call | Result |
|---|---|
| `https://…/` through Caddy | `303` to `/login`, `Server: Caddy` |
| `/health` straight on 8080 | `401` — transport secret missing |
| `/health` with the header, no cookie | `200` — health is deliberately not behind a session |
| `/about` or `/project` with the header, no cookie | `200` — static documents, no session needed |
| `/account/telegram/link` with the header, no cookie | `303` to `/login`; `404` while linking is disabled |
| `/api/command` with the header, no cookie | `401` |
| `/signup` with the header, no cookie | `404` while `SIGNUP_OPEN` is false, `200` once it is |
| `/login` with the header, no cookie | `200` with `Content-Security-Policy`, `X-Frame-Options: DENY` headers |
| `POST /login` with the header, no cookie and no `csrf` field | `400` — the form's token is missing |
| wrong `/login` posts beyond the allowance | `429` `Слишком много попыток. Попробуйте позже.` |
| `/robots.txt` or `/sitemap.xml` with the header, no cookie | `200` — a crawler has no session and cannot get one |

A fresh database logs `Applied 9 new migration(s)` on first start. An existing
one logs only what it is missing, and a deployment with nothing to apply
logs `Database schema is up to date` — in which case **no backup is taken**,
because the dump exists to precede a migration and there was none.

Then open the site in a browser: it must land on the sign-in form, and a wrong
password must return `Incorrect username or password.` without saying which
half was wrong. The login page shows a sign-up link only when
`SIGNUP_OPEN=true` in `.env`; following it and registering should
leave you on the index, already signed in, with a new row in `users`.

Security headers land on every response, so a page should come back with
`Content-Security-Policy`, `X-Frame-Options: DENY` and `X-Content-Type-Options:
nosniff`, and (through Caddy) `Strict-Transport-Security`. Wrong-password posts
are logged as `Failed login attempt for ...`; posting a deliberately wrong
password more often than `LOGIN_ATTEMPTS_PER_WINDOW` times in
`RATE_LIMIT_WINDOW_SECONDS` seconds begins returning `429` with
`Слишком много попыток. Попробуйте позже.`.

The forms carry a CSRF token, so a post made without one is refused — the
login and sign-up forms answer `400` with `Форма устарела. Обновите страницу
и попробуйте снова.`, and a signed-in sign-out or unlink answers `403`. The
changes that matter are logged as audit events: `Signed in`, `Signed out`,
`Account created for`, and `Linked`/`Unlinked` for Telegram, so a successful
action and an attempted one are distinguishable in `journalctl`.

## Search engines

Nothing here is a file. `/robots.txt` and `/sitemap.xml` are routes generated
per request, from `INDEXABLE_PATHS` in `app/web/documents.py`, so `find` will not
turn either one up on disk. Check them over HTTP:

```bash
curl -sI https://<domain>/robots.txt | head -1
curl -s https://<domain>/sitemap.xml
curl -sL https://<domain>/ | grep -c 'href="/about"\|href="/demo"'
```

Expected: `200`; a `<urlset>` listing `/about`, `/project` and `/demo`; and `2`.
Anything else means the response is being answered by the access rules rather
than by the route — a `303` to `/login` on the first two is what an unregistered
path looks like.

`robots.txt` disallows `/` and allows the three public paths, which means it
also disallowed `/sitemap.xml` unless something re-allows it. Google does not
fetch a sitemap that robots.txt disallows, and it reports that as an HTTP
error against a server answering `200` — the sitemap looks perfect from a
terminal and is nevertheless unreadable to the one reader that matters. The
robots file names `/sitemap.xml` explicitly for that reason:

```bash
curl -s https://<domain>/robots.txt
```

Adding a public page means adding its path to `INDEXABLE_PATHS`, and nothing
will report the omission: the page renders and every test passes, while a
crawler never hears about it. The links at the bottom of the sign-in form are
what a crawler follows out of `/`, which redirects there; removing them makes
the whole site unfindable without any visible failure.

The host name in the sitemap comes from `PUBLIC_BASE_URL` in `.env`. It cannot
be read from the `Host` header: the app listens on loopback behind Caddy, and
that header is not the public name of anything.

It has no default, and that is deliberate. A default here would be one
deployment's domain written into every clone of this repository — somebody
else's infrastructure, and a sitemap pointing at a host this installation has
never heard of. Unset, `/sitemap.xml` answers `404`: a file of relative URLs
is not a sitemap, and a console showing an error about a file nobody asked for
is worse than one that is not there. Set it to the name your readers type.

Google still has to be told: register the domain in Search Console and submit
`https://<domain>/sitemap.xml` there. That step cannot be
done from the repository, and until it is, the sitemap is only a suggestion.

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

## Automatic deploy

A push to `main` runs ruff and pytest on a runner and then, if both pass,
`scripts/deploy.sh` on this host through `.github/workflows/deploy.yml`. The workflow lives in the repository; the
server-side half below does not, and has to be set up once.

### Two things both deploy paths depend on

**`uv` has to be reachable by `automation`.** It is installed by its own
installer into `/root/.local/bin`, which is inside a `0700` directory — `root`
sees it in `$PATH`, `automation` cannot enter the path at all. So there is a
copy at `/usr/local/bin/uv`, installed from the root one:

```bash
install -o root -g root -m 755 /root/.local/bin/uv /usr/local/bin/uv
```

One binary, both users: two separate installs would drift apart, and a deploy
that resolves packages under a different `uv` than the manual one does not
mean the same thing as a manual deploy.

It is a copy, not a link, because a link would point back through `/root` and
stop working for `automation`. The cost is that updating `uv` under `root`
leaves this one behind — nothing breaks, it just stops receiving updates.
Re-run the command above after updating.

**The repository belongs to `automation`.** A `git pull` by root writes new
files as root, and the next deploy, running as `automation`, cannot write
there. Pull it as `automation` rather than fixing the ownership afterwards:

```bash
sudo -u automation git pull
```

There is no checkpoint that means "deployed": the job's own log is the record,
and the script ends by printing the last 50 journal lines.

### Dependencies

Dependabot (`.github/dependabot.yml`) opens a pull request every Monday for
the packages in `uv.lock`: minor and patch updates together, each major
version on its own. A pull request runs the test job — lint, audit, tests —
and never deploys; merging it is a push to `main` and deploys like any other.
The audit fails the job, and so the deploy, on any published vulnerability in
a locked package.

### What it does, and what it does not claim

The key stored in GitHub is restricted to one script, but that is not the
boundary worth relying on. The script is in the repository, so whoever can
push to `main` can change what it runs — and pushing code to this repository
already means that code runs on this server, because the deploy starts it.
The automation removes the waiting, not the permission.

**There is no separation of duties here, and pretending otherwise would be
the useful lie to tell.** This is one person's repository: the owner is the
one pushing, and the owner is the one whose account would have to be
compromised for anything else to get in. So the honest statement of the
boundary is the GitHub account itself.

A ruleset on `main` is configured anyway, with no bypass and no pull request
requirement — the fast path was worth more than the ceremony. What it actually
buys:

- force pushes and branch deletion are refused, so a mistake costs a reflog
  instead of the history;
- the rules apply to everyone, which means a second collaborator or a token
  added later is covered without revisiting this;
- there is a place where "main must go through a pull request" becomes a
  setting rather than an intention.

What it does not buy is review before deploy. For that, the rule would need
the owner *out* of the bypass list — and then the owner, alone in the
repository, cannot merge their own pull request, because the author may not
approve it. There is no configuration that gives one person both.

### Server setup, once

**1. A shell for the deploy user.** `automation` is `nologin`, and an SSH
connection with a forced command still needs a shell to run it:

```bash
usermod -s /bin/bash automation
```

**2. Two privileged commands, and nothing else.** The restart is what the
deploy cannot do itself, and reading the journal back is what a user who is not
on the host cannot do either. Write `/etc/sudoers.d/automation-deploy` with
mode `440`:

```
automation ALL=(root) NOPASSWD: /usr/bin/systemctl restart automation-platform
automation ALL=(root) NOPASSWD: /usr/bin/journalctl -u automation-platform -n 50 --no-pager
```

Every argument is spelled out, with no wildcard, so each rule is one command
and nothing more. Validate with `visudo -c` before saving — a sudoers file
that does not parse can lock the whole host out of sudo.

`automation` is not in `systemd-journal`, and that is deliberate. Adding it is
one command instead of a rule, and it grants read access to every log on the
machine, permanently, including the ones written after the deploy is gone.
The wildcard-free rules read one unit's last 50 lines and stop.

The arguments are a tripwire in both directions: `sudo` matches them exactly,
so changing `-n 50` in `scripts/deploy.sh` without changing the rule breaks the
deploy. A test checks that the two files agree.

**3. A key that may only run the deploy.** Generate it on the runner side, not
here: a key that has ever been written to this host should be considered
spent. Then, in `~automation/.ssh/authorized_keys`:

```
command="/opt/automation-platform/scripts/deploy.sh",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty <public key>
```

`no-pty` matters as much as the forced command: without it the key can open an
interactive shell that does not run the forced command at all.

The script must exist before the first automated deploy, because the key
points at a path inside the checkout. Pull once by hand the first time.

**4. The host key, pinned.** Read it here and put it in the GitHub secret as
`DEPLOY_KNOWN_HOSTS`:

```bash
ssh-keyscan -p 22 <domain>
```

Scanning it at deploy time instead would mean whichever machine answered that
day decides who this host is for ever after, and that machine is not under
this host's control.

### GitHub secrets

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | `<domain>` |
| `DEPLOY_USER` | `automation` |
| `DEPLOY_KEY` | the private key, base64-encoded (see below) |
| `DEPLOY_KNOWN_HOSTS` | the output of `ssh-keyscan` above |

`DEPLOY_KEY` is stored base64-encoded, not as the `BEGIN`/`END` block:

```bash
base64 < ~/.ssh/platform_deploy | tr -d '\n'
```

To produce a replacement after rotating the key. A multi-line block pasted
into a secret field comes back with its line endings changed or dropped, and
OpenSSH rejects it with `error in libcrypto` — which names neither the line
nor the cause, in a log whose whole subject is the key. One line of base64
has one representation, so it cannot be mangled on the way.

### The script pulls, then runs itself again

The pull can rewrite `scripts/deploy.sh` — it lives in the repository it
updates — while bash is reading it. Bash reads scripts by offset from an open
handle, so what executes next depends on where the new bytes happen to line up.
A three-line script that overwrites itself with a shorter one prints its first
line and stops.

That is silent. The service is up, the pull succeeded, the log says the deploy
worked, and the restart never happened.

So the script pulls, then `exec`s itself. The second pass re-opens the file and
runs the version that was just pulled — the one whose code it is about to start.
An argument records that the pull already happened, and the pull sits inside
the guard so the second pass cannot reach it and loop.

### Checking a deploy

```bash
gh run list --limit 5          # from the checkout, with gh authenticated
```

After the restart `deploy.sh` runs `python -m app.manage verify` as
`automation`. It waits up to 90 s for `/health` on the loopback port, runs
every `/project/trace` scenario, then each enabled plugin's page or read-only
`check` command against the production database, under the guest identity
`web:guest:verify` (pruned nightly with the other guests). Each line of its
report is in the workflow log; any failure exits non-zero and turns the
`deploy` job red. It does not roll back: the new version is already running.

On the host, the last 50 lines of the service journal show the migrations
that ran and whether the service came back. A deploy that applied a migration
also wrote a dump first — see "Search engines" for the `up to date` case, which
means no dump and nothing to apply.

### Stopping it

Deleting the line from `~automation/.ssh/authorized_keys` is the whole
switch: the workflow will fail with a permission error on the next push and
nothing on the host changes.


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

1. DNS above must agree on `<ip>`, otherwise the proxy cannot reach
   the VPS at all.
2. IT must add `<domain>` to the allowlist for HTTPS
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

### The copy off the host

A dump on the database's own disk does not survive losing the server. With
`BACKUP_PASSPHRASE` set in `.env`, each scheduled run also sends the dump to
every active administrator with a linked Telegram chat, encrypted with
symmetric gpg (AES256). Keep the passphrase outside this server, in a password
manager: without it the copies cannot be opened. `gpg` ships with Ubuntu.

A failed copy fails the unit (`Result=exit-code`), and the local dump is
already written by then. To open a copy:

```bash
gpg -d scheduled_<timestamp>.sql.gpg > scheduled_<timestamp>.sql
```

Then restore it as below.

## Demonstration cleanup

The public demonstration at `/demo` gives every visitor a namespace of their
own, and none of them is an account: no row in `users`, no session, no
password. What a visitor enters lives under `web:guest:<token>`, so clearing
them is a delete by prefix.

`/etc/systemd/system/automation-platform-guests.service`:

```ini
[Unit]
Description=Automation Platform demonstration cleanup
After=postgresql.service

[Service]
Type=oneshot
User=automation
WorkingDirectory=/opt/automation-platform
ExecStart=/opt/automation-platform/.venv/bin/python -m app.manage prune-guests
```

The visit log is cleaned the same way, with its own unit and an hour later,
so the two jobs never overlap:

`/etc/systemd/system/automation-platform-visits.service`:

```ini
[Unit]
Description=Automation Platform visit log cleanup
After=postgresql.service

[Service]
Type=oneshot
User=automation
WorkingDirectory=/opt/automation-platform
ExecStart=/opt/automation-platform/.venv/bin/python -m app.manage prune-visits
```

`/etc/systemd/system/automation-platform-visits.timer`:

```ini
[Unit]
Description=Daily Automation Platform visit log cleanup

[Timer]
OnCalendar=*-*-* 05:47:00
Persistent=true

[Install]
WantedBy=timers.target
```

The window is `RETENTION_DAYS` in `app/db/visits_repo.py` (90 by default), so
the first run on an empty log removes nothing — which is a normal result.

```bash
systemctl daemon-reload
systemctl enable --now automation-platform-visits.timer
systemctl start automation-platform-visits.service
journalctl -u automation-platform-visits.service -n 20 --no-pager
systemctl list-timers automation-platform-visits.timer
```

`/etc/systemd/system/automation-platform-guests.timer`:

```ini
[Unit]
Description=Daily Automation Platform demonstration cleanup

[Timer]
OnCalendar=*-*-* 05:23:00
Persistent=true

[Install]
WantedBy=timers.target
```

An hour after the backup, and at an odd minute, so neither job is the thing
that is running when the box is busiest.

```bash
systemctl daemon-reload && systemctl enable --now automation-platform-guests.timer
systemctl start automation-platform-guests.service
journalctl -u automation-platform-guests.service -n 20 --no-pager
systemctl list-timers automation-platform-guests.timer
```

Running it by hand is the check: open `/demo/expenses` first, add something,
run the job, and confirm the namespace is empty. **That is expected and not a
fault** — a demonstration is seeded again on the next arrival, so a visitor
who comes back finds the sample data rather than their own. Only what they
entered is gone, which is the whole promise of the page.

It reports a count per table:

```
expenses: 4
expense_categories: 2
job_applications: 1
```

All zeros is a normal result for a day nobody opened the demonstration.

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

## Local environment

Only for the machine that develops this, never for a server.

**The local `.env` must hold a development bot token.** The production token
lives in the server's `.env` and has no business on a laptop: two pollers on one
Telegram bot fight, Telegram answers the second `getUpdates` with
`Conflict: terminated by other getUpdates request`, and the production bot
stops fetching for as long as the local one runs. It has happened.

The token is not optional either. The application refuses to start without
`BOT_TOKEN`, and a well-formed token that Telegram rejects kills the process
when polling fails — the web interface goes down with it.

So that nobody has to remember this, a check sits next to the repository. From
the main checkout:

```bash
../automation-platform.agent/check-local-bot.sh
```

It asks Telegram which bot the local token belongs to and exits non-zero when
the answer is the production one. Run it before `python -m app.main`, not after.

Local PostgreSQL on the default port is the other requirement; migrations are
applied on start, so an empty database is enough.

**A local run needs a working route to Telegram.** The application polls the bot
and takes the web interface down with it when polling fails, so a machine that
cannot reach `api.telegram.org` cannot serve the console either. Behind a
provider that blocks it, that means the stand only starts with a proxy on, and
the check above will time out before it can answer — it is reporting on the
network, not on the token.

The sibling `automation-platform.agent/` directory holds that script and the log
of a local run. It is outside the repository and untracked on purpose:
tooling that only makes sense on one machine does not belong in a project other
people read.

## Local checks

```bash
uv run ruff check .
uv run pytest
```

782 tests. Run before pushing anything that touches config, auth or
migrations. The parts worth knowing about:

- `tests/test_config.py` — subprocess test for the `.env` import-order bug;
  it fails if that fix is reverted, because a module-level ordering problem
  is invisible to in-process tests.
- `tests/test_auth.py` — the transport secret and the session separately; the
  CSRF token on the anonymous and session-backed forms, the rate limit and its
  reset on a successful login, the sign-up gate, and that sign-up is off by
  default unless `SIGNUP_OPEN` is set.
- `tests/test_telegram_auth.py` and `tests/test_telegram_link.py` — the Login
  Widget signature, the one-use nonce, and the link/unlink flow including the
  theft a nonce exists to block.
- `tests/test_delivery.py` and `tests/test_notifications.py` — where a linked
  account's reminder actually goes, that an undeliverable one is given up on
  after three passes, and that a web notification's text is kept out of the log.
- `tests/test_person.py` and `tests/test_notes_deletion.py` — reads spanning a
  linked pair, and `/del` addressed by position, never by row id.
- `tests/test_accounts.py` — password storage, session lifetime, and that two
  web users cannot read each other's notes.
- `tests/test_passwords.py` — a corrupt or truncated hash fails the login
  instead of raising.
