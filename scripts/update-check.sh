#!/usr/bin/env bash
set -euo pipefail

REPO=/opt/automation-platform
REBOOT_FLAG=${REBOOT_FLAG:-/var/run/reboot-required}
UU_LOG=${UU_LOG:-/var/log/unattended-upgrades/unattended-upgrades.log}

problems=()

result=$(systemctl show -p Result --value apt-daily-upgrade.service 2>/dev/null || echo unknown)
if [[ "$result" != "success" ]]; then
    problems+=("последний запуск автообновления завершился с ошибкой: ${result}")
fi

cutoff=$(date -d "24 hours ago" "+%Y-%m-%d %H:%M:%S")
errors=$(awk -v cutoff="$cutoff" '$0 >= cutoff && / ERROR /' "$UU_LOG" 2>/dev/null | grep -c . || true)
if (( errors > 0 )); then
    problems+=("в журнале автообновления ошибок за сутки: ${errors}")
fi

pending=$(apt list --upgradable 2>/dev/null | grep -c -- "-security" || true)
if (( pending > 0 )); then
    problems+=("не установлено обновлений безопасности: ${pending}")
fi

if [[ -f "$REBOOT_FLAG" ]]; then
    age=$(( $(date +%s) - $(stat -c %Y "$REBOOT_FLAG") ))
    if (( age > 86400 )); then
        problems+=("сервер ждёт перезагрузки дольше суток")
    fi
fi

livepatch=$(pro status --format json 2>/dev/null \
    | python3 -c 'import json, sys; print(next((s["status"] for s in json.load(sys.stdin)["services"] if s["name"] == "livepatch"), "absent"))' \
    2>/dev/null || echo unknown)
if [[ "$livepatch" != "enabled" ]]; then
    problems+=("Livepatch не работает: ${livepatch}")
fi

if (( ${#problems[@]} == 0 )); then
    if [[ "${1:-}" == "--print" ]]; then
        printf 'Обновления безопасности: всё в порядке\n'
    fi
    exit 0
fi

message="Обновления безопасности: нужна проверка"
for problem in "${problems[@]}"; do
    message+=$'\n'"- ${problem}"
done

if [[ "${1:-}" == "--print" ]]; then
    printf '%s\n' "$message"
    exit 0
fi

cd "$REPO"
printf '%s\n' "$message" | runuser -u automation -- \
    "$REPO/.venv/bin/python" -m app.manage notify-admins
