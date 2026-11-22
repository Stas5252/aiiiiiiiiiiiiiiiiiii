#!/usr/bin/env bash
# Обновление LeadHunter: забирает свежий код и перезапускает бота. Настройки и база (.env, data/) не трогаются.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf '❌ %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
as_root() { if [ -n "$SUDO" ]; then sudo "$@"; else "$@"; fi; }

if ! command -v git >/dev/null 2>&1; then die "Нужен git: без него обновления не скачать (на Ubuntu: apt install git)."; fi
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    die "Эта папка скачана архивом, а не через git, — обновить её нельзя. Скачайте проект заново (git clone, адрес — в README), перенесите в новую папку файл .env и папку data/ и запустите там ./install.sh"
fi

echo "⬇️  Забираю обновления…"
if ! out="$(git fetch 2>&1)"; then
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    die "Не удалось связаться с сервером обновлений: нет интернета или доступа к репозиторию. Проверьте связь и запустите снова."
fi
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
if [ -z "$upstream" ]; then die "Эта копия не привязана к ветке на сервере (git status покажет, где вы). Переключитесь на основную ветку и запустите снова."; fi
if ! out="$(git merge --ff-only "$upstream" 2>&1)"; then
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    changed="$(git status --porcelain --untracked-files=no | cut -c4- | head -5 | tr '\n' ' ')"
    if [ -n "$(git rev-list "$upstream..HEAD" | head -1)" ]; then
        die "Не удалось обновить: в папке есть свои коммиты, которых нет на сервере. Нужен git rebase или свежая копия проекта."
    elif [ -n "$changed" ]; then
        die "Не удалось обновить: в папке есть ваши правки кода (${changed% }). Отложите их (git stash) и запустите снова."
    fi
    die "Не удалось обновить (причина — в сообщении git выше)."
fi

# .env из старого образца: «KEY=   # комментарий». Docker Compose передаёт такой комментарий боту как значение.
if [ -f .env ] && grep -qE '^[A-Za-z_][A-Za-z0-9_]*=[[:space:]]+#' .env; then
    tmp="$(mktemp)"
    sed -E 's/^([A-Za-z_][A-Za-z0-9_]*)=[[:space:]]+#.*$/\1=/' .env > "$tmp"
    cat "$tmp" > .env; rm -f "$tmp"
fi

DOCKER_CMD=""
dk() { if [ "$DOCKER_CMD" = "sudo docker" ]; then sudo docker "$@"; else docker "$@"; fi; }
if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then DOCKER_CMD="docker"
    elif [ -n "$SUDO" ] && sudo docker info >/dev/null 2>&1; then DOCKER_CMD="sudo docker"; fi
fi

BAD_ENV="Новая версия не принимает настройки из .env (что именно — выше). Бот не перезапущен и работает на старой версии. Исправьте .env и запустите ./update.sh снова."

if [ -n "$DOCKER_CMD" ] && [ -n "$(dk compose ps -a -q 2>/dev/null || true)" ]; then
    dk compose build || die "Docker не смог собрать новую версию (ошибка выше). Бот работает на старой."
    dk compose run --rm --no-deps -T leadhunter python -m leadhunter.config || die "$BAD_ENV"
    dk compose up -d
    echo "✅ Обновлено (Docker). Логи: $DOCKER_CMD compose logs -f"
elif [ -x .venv/bin/pip ]; then
    .venv/bin/pip install -q . || die "Зависимости не обновились (ошибка выше). Проверьте интернет и запустите снова."
    .venv/bin/python -m leadhunter.config || die "$BAD_ENV"
    if command -v systemctl >/dev/null 2>&1 && systemctl cat leadhunter.service >/dev/null 2>&1; then
        as_root systemctl restart leadhunter || die "Код обновлён, но службу перезапустить не удалось. Попробуйте: systemctl restart leadhunter"
        echo "✅ Обновлено, служба перезапущена. Логи: journalctl -u leadhunter -f"
    else
        echo "✅ Код обновлён. Перезапустите бота: остановите его (Ctrl+C) и запустите заново: .venv/bin/python -m leadhunter"
    fi
else
    die "Не нашёл ни Docker-контейнера, ни .venv. Сначала запустите ./install.sh"
fi
