#!/usr/bin/env bash
#
# The morning SSH digest: the last day's failed logins, who got in, and what
# fail2ban banned, sent to the administrators' Telegram chats.
#
# Runs as root, because the journal and fail2ban answer only to root. It is
# therefore NOT run from the checkout: `automation` owns the checkout, and a
# root job reading a file that user can rewrite would hand root to anything
# that compromises the application. Copy it into place by hand, as root, and
# copy it again deliberately after a change — see docs/DEPLOYMENT.md.
#
# The sending runs as `automation` through `app.manage notify-admins`, so the
# bot token and the database are read by the user that already holds them.
#
# `--print` prints the message instead of sending it.

set -euo pipefail

REPO=/opt/automation-platform
SINCE="24 hours ago"

log=$(journalctl -u ssh --since "$SINCE" --no-pager -o cat)

# No `head` anywhere below. systemd starts services with SIGPIPE ignored, so
# when `head` closes the pipe early the writer before it gets an error instead
# of a quiet death — `sort: write failed: Broken pipe`, exit 2 — and pipefail
# takes the whole run down. It only shows once a day's log is long enough to
# outgrow the pipe. Awk reads to the end and keeps only what it prints.

# One line per refused connection: sshd closes each with "[preauth]" naming an
# invalid or an authenticating user.
refused=$(grep -E "(Connection closed by|Disconnected from) (invalid|authenticating) user .*\[preauth\]" <<<"$log" || true)
attempts=$(grep -c . <<<"$refused" || true)
addresses=$(grep -oE "[0-9]{1,3}(\.[0-9]{1,3}){3} port" <<<"$refused" | sort -u | grep -c . || true)
top_users=$(awk '{for (i = 1; i < NF; i++) if ($i == "user") {print $(i + 1); break}}' <<<"$refused" \
    | sort | uniq -c | sort -rn \
    | awk 'NR <= 3 {printf "%s%s (%s)", sep, $2, $1; sep = ", "}')

# "Accepted publickey for root from 1.2.3.4 port 5 ssh2: ..."
accepted=$(awk '$1 == "Accepted" {print $4, $6, $2}' <<<"$log" | sort | uniq -c | sort -rn \
    | awk '{printf "  %s с %s, %s ×%s\n", $2, $3, $4, $1}')

cutoff=$(date -d "$SINCE" "+%Y-%m-%d %H:%M:%S")
banned_today=$(awk -v cutoff="$cutoff" '$0 >= cutoff && / Ban /' /var/log/fail2ban.log 2>/dev/null | grep -c . || true)
banned_now=$(fail2ban-client status sshd 2>/dev/null | awk -F: '/Currently banned/ {gsub(/[ \t]/, "", $2); print $2}')

message="SSH за сутки
Неудачных подключений: ${attempts} с ${addresses} адресов
Чаще всего пробовали: ${top_users:-—}
Забанено fail2ban за сутки: ${banned_today}, в бане сейчас: ${banned_now:-?}
Успешные входы:
${accepted:-  нет}"

if [[ "${1:-}" == "--print" ]]; then
    printf '%s\n' "$message"
    exit 0
fi

cd "$REPO"
printf '%s\n' "$message" | runuser -u automation -- \
    "$REPO/.venv/bin/python" -m app.manage notify-admins
