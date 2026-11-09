#!/usr/bin/env bash
# Обновление LeadHunter: забирает свежий код и перезапускает бота. Настройки и база (.env, data/) не трогаются.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "⬇️  Забираю обновления…"
if ! git pull --ff-only; then
    echo "❌ Не удалось обновить: в папке есть ваши правки кода. Отмените их (git stash) и запустите снова." >&2
    exit 1
fi

DOCKER=""
if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then DOCKER="docker"
    elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then DOCKER="sudo docker"; fi
fi

if [ -n "$DOCKER" ] && [ -n "$($DOCKER compose ps -q 2>/dev/null || true)" ]; then
    $DOCKER compose up -d --build
    echo "✅ Обновлено (Docker). Логи: $DOCKER compose logs -f"
elif [ -x .venv/bin/pip ]; then
    .venv/bin/pip install -q .
    if command -v systemctl >/dev/null 2>&1 && systemctl cat leadhunter.service >/dev/null 2>&1; then
        sudo systemctl restart leadhunter
        echo "✅ Обновлено, служба перезапущена. Логи: journalctl -u leadhunter -f"
    else
        echo "✅ Код обновлён. Перезапустите бота: остановите его (Ctrl+C) и запустите заново: .venv/bin/python -m leadhunter"
    fi
else
    echo "❌ Не нашёл ни запущенного Docker-контейнера, ни .venv. Сначала запустите ./install.sh" >&2
    exit 1
fi
