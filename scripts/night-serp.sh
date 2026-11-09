#!/usr/bin/env bash
# Ночной сбор выдачи: Яндекс принимает отложенные запросы только с 00:00 до 06:00 МСК.
# Запуск: bash scripts/night-serp.sh [домен]. Домен по умолчанию берётся из seo.toml.
# На macOS добавьте в начало caffeinate -i, чтобы компьютер не уснул до ночи.
set -e
site=()
if [ -n "${1:-}" ]; then site=("$1"); fi
cd "$(dirname "$0")/.."

seconds=$(python3 -c "
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
now = datetime.now(ZoneInfo('Europe/Moscow'))
start = now.replace(hour=0, minute=5, second=0, microsecond=0)
if now >= start and now.hour < 6:
    print(0)
else:
    print(int((start + timedelta(days=1) - now).total_seconds()) if now >= start else int((start - now).total_seconds()))
")
echo "ожидание до 00:05 МСК: $seconds с"
sleep "$seconds"

.venv/bin/seo serp-submit "${site[@]}" --yes
.venv/bin/seo serp-fetch "${site[@]}" --wait 360
.venv/bin/seo competitors "${site[@]}"
