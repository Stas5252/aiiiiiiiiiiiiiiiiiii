#!/usr/bin/env bash
# Установщик LeadHunter «для чайников»: спросит токен бота, поставит всё нужное и запустит.
# Запуск:  curl -fsSL https://raw.githubusercontent.com/Stas5252/aiiiiiiiiiiiiiiiiiii/main/install.sh | bash
# Для автоматики (без вопросов): BOT_TOKEN=... [GEMINI_API_KEY=...] LEADHUNTER_YES=1 ./install.sh
# Ещё переменные: LEADHUNTER_NO_DOCKER=1 — ставить через Python, Docker не трогать;
#   LEADHUNTER_ENV_ONLY=1 — только подготовить .env; LEADHUNTER_SKIP_NET=1 — не проверять токен в Telegram;
#   LEADHUNTER_DIR, LEADHUNTER_REPO, LEADHUNTER_BRANCH — куда и откуда скачивать код
#   (LEADHUNTER_BRANCH при повторном запуске переключает уже скачанный проект на эту ветку).
set -euo pipefail

REPO_URL="${LEADHUNTER_REPO:-https://github.com/Stas5252/aiiiiiiiiiiiiiiiiiii.git}"
BRANCH="${LEADHUNTER_BRANCH:-}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
YES="${LEADHUNTER_YES:-}"
# Только для тестов: папка вместо корня системы (там ищем etc/os-release, run/systemd, etc/systemd)
ROOT="${LEADHUNTER_ROOT:-}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

# Ввод идёт из терминала, даже когда скрипт запущен через `curl | bash`.
tty_read() { # tty_read <silent:0|1> <prompt> -> REPLY
    local silent="$1" prompt="$2"
    REPLY=""
    if [ -n "$YES" ] || [ ! -r /dev/tty ]; then return 0; fi
    if [ "$silent" = 1 ]; then read -rs -p "$prompt" REPLY </dev/tty || true; echo
    else read -r -p "$prompt" REPLY </dev/tty || true; fi
}

confirm() { # confirm <вопрос> [по умолчанию y|n]
    local def="${2:-y}"
    if [ -n "$YES" ]; then [ "$def" = y ]; return; fi
    tty_read 0 "$1 [$([ "$def" = y ] && echo Д/н || echo д/Н)] "
    case "$REPLY" in
        д|Д|да|Да|ДА|y|Y|yes|Yes|YES) return 0 ;;
        н|Н|нет|Нет|НЕТ|n|N|no|No|NO) return 1 ;;
        *) [ "$def" = y ] ;;
    esac
}

SUDO=""
CAN_ROOT=0
if [ "$(id -u)" -eq 0 ]; then CAN_ROOT=1
elif command -v sudo >/dev/null 2>&1; then SUDO="sudo"; CAN_ROOT=1; fi
as_root() { if [ -n "$SUDO" ]; then sudo "$@"; else "$@"; fi; }

OS="other"
case "$(uname -s)" in
    Darwin) OS="mac" ;;
    Linux)  if [ -r "$ROOT/etc/os-release" ] && grep -qiE 'ubuntu|debian' "$ROOT/etc/os-release"; then OS="debian"; else OS="linux"; fi ;;
esac

APT_UPDATED=0
apt_install() { # apt_install <пакеты…>; 1 — если поставить не вышло
    if [ "$CAN_ROOT" != 1 ]; then
        warn "Чтобы поставить ($*), нужны права администратора: запустите установщик от root или установите sudo."
        return 1
    fi
    if [ "$APT_UPDATED" = 0 ]; then
        as_root env DEBIAN_FRONTEND=noninteractive apt-get update -y -q >/dev/null || warn "apt-get update не прошёл — пробую ставить так."
        APT_UPDATED=1
    fi
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@"
}

ensure_curl() {
    if command -v curl >/dev/null 2>&1; then return 0; fi
    [ "$OS" = debian ] || return 1
    confirm "Нужен curl (им скачиваются файлы). Установить через apt?" y || return 1
    apt_install curl ca-certificates >/dev/null || return 1
    command -v curl >/dev/null 2>&1
}

# ---------- где лежит проект ----------
on_branch() { [ "$(git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || true)" = "$BRANCH" ]; }

switch_branch() { # switch_branch <папка> — переключить скачанную копию на ветку $BRANCH
    local dir="$1"
    git -C "$dir" remote set-branches origin "$BRANCH" || return 1
    if git -C "$dir" show-ref --verify --quiet "refs/heads/$BRANCH"; then
        # Ветка в папке уже есть: свои коммиты в ней не трогаем, только догоняем сервер
        git -C "$dir" fetch origin "$BRANCH" || return 1
        git -C "$dir" checkout -q "$BRANCH" || return 1
        git -C "$dir" branch -q --set-upstream-to="origin/$BRANCH" "$BRANCH" || return 1
        git -C "$dir" merge -q --ff-only "origin/$BRANCH"
    else
        # Папка скачана с другой ветки (и только с ней: --depth 1)
        git -C "$dir" fetch --depth 1 origin "$BRANCH" || return 1
        git -C "$dir" checkout -q -b "$BRANCH" --track "origin/$BRANCH"
    fi
}

if [ -z "$SELF_DIR" ] || [ ! -f "$SELF_DIR/pyproject.toml" ] || [ ! -d "$SELF_DIR/leadhunter" ]; then
    say "Скачиваю LeadHunter"
    if ! command -v git >/dev/null 2>&1; then
        [ "$OS" = debian ] || die "Установите git и запустите снова."
        confirm "Нужен git. Установить через apt?" y || die "Без git не получится."
        apt_install git ca-certificates || die "Не удалось поставить git."
    fi
    TARGET="${LEADHUNTER_DIR:-$HOME/leadhunter}"
    if [ -d "$TARGET/.git" ] && [ -n "$BRANCH" ] && ! on_branch "$TARGET"; then
        switch_branch "$TARGET" \
            || die "Не удалось переключить папку $TARGET на ветку $BRANCH (причина — выше). Проверьте название ветки и интернет."
    elif [ -d "$TARGET/.git" ]; then
        git -C "$TARGET" pull --ff-only || warn "Не удалось обновить, использую то, что есть."
    elif [ -n "$BRANCH" ]; then
        git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TARGET" \
            || die "Не удалось скачать ветку $BRANCH из $REPO_URL. Проверьте интернет и адрес."
    else
        git clone --depth 1 "$REPO_URL" "$TARGET" \
            || die "Не удалось скачать $REPO_URL. Проверьте интернет; если репозиторий закрытый — нужен доступ к нему."
    fi
    [ -f "$TARGET/install.sh" ] \
        || die "В скачанной папке $TARGET нет install.sh (в этой ветке нет кода). Укажите ветку: LEADHUNTER_BRANCH=dev"
    exec bash "$TARGET/install.sh" "$@"
fi
cd "$SELF_DIR"

# Запуск из самой папки проекта с другой веткой (например, прежнюю ветку на сервере удалили)
if [ -n "$BRANCH" ] && [ -d .git ] && command -v git >/dev/null 2>&1 && ! on_branch "$SELF_DIR"; then
    say "Переключаю проект на ветку $BRANCH"
    switch_branch "$SELF_DIR" \
        || die "Не удалось переключить проект на ветку $BRANCH (причина — выше). Проверьте название ветки и интернет."
    exec bash "$SELF_DIR/install.sh" "$@"
fi

# ---------- .env ----------
# Значение передаётся через окружение awk: не попадает ни в вывод, ни в аргументы процессов.
set_env() { # set_env KEY VALUE
    local tmp; tmp="$(mktemp)"
    V="$2" awk -v k="$1" 'BEGIN{done=0} index($0,k"=")==1 && !done {print k"="ENVIRON["V"]; done=1; next} {print} END{if(!done) print k"="ENVIRON["V"]}' .env > "$tmp"
    cat "$tmp" > .env; rm -f "$tmp"
}
# Значение без комментария в конце и без кавычек вокруг — так же его читают бот и Docker
get_env() {
    { grep -E "^$1=" .env 2>/dev/null || true; } | head -1 | cut -d= -f2- \
        | sed -e 's/[[:space:]]*#.*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

# .env из старого образца: «KEY=   # комментарий». Docker Compose передаёт такой комментарий боту как значение.
fix_env_comments() {
    local tmp; tmp="$(mktemp)"
    sed -E 's/^([A-Za-z_][A-Za-z0-9_]*)=[[:space:]]+#.*$/\1=/' .env > "$tmp"
    cat "$tmp" > .env; rm -f "$tmp"
}

valid_token() { [[ "$1" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]]; }

setup_env() {
    say "Настройка бота"
    [ -f .env ] || cp .env.example .env
    chmod 600 .env 2>/dev/null || true
    fix_env_comments

    local token="${BOT_TOKEN:-}" current; current="$(get_env BOT_TOKEN)"
    if [ -z "$token" ] && valid_token "$current"; then
        if confirm "Токен бота в .env уже есть. Оставить его?" y; then token="$current"; fi
    fi
    if [ -z "$token" ]; then
        echo "Токен выдаёт @BotFather в Telegram: напишите ему /newbot, придумайте имя — он пришлёт строку вида 123456:ABC…"
        echo "(ввод скрыт — просто вставьте токен и нажмите Enter)"
        while :; do
            tty_read 1 "Токен бота: "; token="${REPLY// /}"
            valid_token "$token" && break
            if [ -n "$YES" ] || [ ! -r /dev/tty ]; then die "Токен не задан или неверного формата. Передайте BOT_TOKEN=..."; fi
            warn "Это не похоже на токен (нужно 123456:AAA…). Попробуйте ещё раз."
        done
    fi
    valid_token "$token" || die "BOT_TOKEN неверного формата."
    set_env BOT_TOKEN "$token"

    if command -v curl >/dev/null 2>&1 && [ -z "${LEADHUNTER_SKIP_NET:-}" ]; then
        local name=""
        name="$(curl -fsS --max-time 10 "https://api.telegram.org/bot${token}/getMe" 2>/dev/null | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')" || true
        if [ -n "$name" ]; then ok "Токен принят: @$name"; else warn "Не смог проверить токен (нет сети или токен неверный) — продолжаю."; fi
    fi

    local gem="${GEMINI_API_KEY:-}"
    if [ -z "$gem" ] && [ -z "$(get_env GEMINI_API_KEY)" ]; then
        echo
        echo "Ключ Gemini (ИИ для текстов и озвучки) — бесплатно: aistudio.google.com → Get API key."
        echo "Можно пропустить (Enter) и добавить позже в боте командой /setup."
        tty_read 1 "Ключ Gemini: "; gem="${REPLY// /}"
    fi
    if [ -n "$gem" ]; then set_env GEMINI_API_KEY "$gem"; fi

    # Кабинет: бесплатный туннель, если владелец не задал свой адрес или другой режим
    if [ -z "$(get_env MINIAPP_URL)" ] && [ -z "$(get_env MINIAPP_TUNNEL)" ]; then set_env MINIAPP_TUNNEL "cloudflare"; fi
    ok ".env готов (права 600, ключи не показываются)"
}

# ---------- Docker ----------
DOCKER_CMD=""
dk() { if [ "$DOCKER_CMD" = "sudo docker" ]; then sudo docker "$@"; else docker "$@"; fi; }
docker_ready() {
    command -v docker >/dev/null 2>&1 || return 1
    if docker info >/dev/null 2>&1; then DOCKER_CMD="docker"
    elif [ -n "$SUDO" ] && sudo docker info >/dev/null 2>&1; then DOCKER_CMD="sudo docker"
    else return 1; fi
    dk compose version >/dev/null 2>&1
}

install_docker() { # 1 — если Docker поставить не вышло (тогда ставим через Python)
    ensure_curl || { warn "Нет curl — Docker поставить не могу."; return 1; }
    if [ "$CAN_ROOT" != 1 ]; then warn "Для установки Docker нужны права администратора (root или sudo)."; return 1; fi
    say "Ставлю Docker (официальный скрипт get.docker.com, 1–3 минуты)"
    local script; script="$(mktemp)"
    if ! curl -fsSL -o "$script" https://get.docker.com; then
        rm -f "$script"; warn "Не удалось скачать установщик Docker (нет сети?)."; return 1
    fi
    if ! as_root sh "$script"; then rm -f "$script"; warn "Docker не установился."; return 1; fi
    rm -f "$script"
    if [ -n "$SUDO" ]; then sudo usermod -aG docker "$(id -un)" 2>/dev/null || true; fi
}

STARTED=0
run_docker() {
    say "Собираю образ Docker (первая сборка занимает 3–10 минут)"
    dk compose build || die "Docker не смог собрать образ (ошибка выше). Можно поставить без Docker: LEADHUNTER_NO_DOCKER=1 ./install.sh"
    dk compose run --rm --no-deps -T leadhunter python -m leadhunter.config \
        || die "Бот не принимает настройки из .env (что именно — выше). Исправьте файл и запустите ./install.sh снова."
    dk compose up -d || die "Не удалось запустить контейнер (ошибка выше)."
    STARTED=1
    ok "Контейнер запущен"
    echo "  Логи: $DOCKER_CMD compose logs -f    Остановить: $DOCKER_CMD compose down"
}

# ---------- Python-режим ----------
python_ok() { command -v "$1" >/dev/null 2>&1 && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

find_python() {
    local p
    for p in python3.13 python3.12 python3.11 python3; do
        if python_ok "$p"; then echo "$p"; return 0; fi
    done
    return 1
}

make_venv() { # make_venv <python>
    [ -x .venv/bin/pip ] && return 0
    rm -rf .venv
    if "$1" -m venv .venv >/dev/null 2>&1 && [ -x .venv/bin/pip ]; then return 0; fi
    rm -rf .venv
    [ "$OS" = debian ] || return 1
    # На Ubuntu/Debian модуль venv лежит в отдельном пакете
    apt_install python3-venv >/dev/null || return 1
    "$1" -m venv .venv && [ -x .venv/bin/pip ]
}

run_python() {
    say "Устанавливаю без Docker (Python)"
    local py=""
    py="$(find_python)" || true
    if [ -z "$py" ]; then
        case "$OS" in
            debian) confirm "Нужен Python 3.11+. Установить через apt?" y || die "Без Python не получится."
                    apt_install python3 python3-venv python3-pip >/dev/null || die "Не удалось поставить Python." ;;
            mac)    command -v brew >/dev/null 2>&1 || die "Нужен Homebrew (https://brew.sh) или Docker Desktop (https://docker.com/products/docker-desktop)."
                    confirm "Нужен Python 3.12. Установить через brew?" y || die "Без Python не получится."
                    brew install python@3.12 ;;
            *)      die "Установите Python 3.11+ и запустите снова." ;;
        esac
        py="$(find_python)" || die "Нужен Python 3.11 или новее (на вашей системе он старый). Проще поставить Docker и запустить установщик снова."
    fi
    ok "Python: $("$py" --version)"

    if ! command -v ffmpeg >/dev/null 2>&1 || { [ "$OS" = debian ] && ! dpkg -s fonts-dejavu-core >/dev/null 2>&1; }; then
        case "$OS" in
            debian) if confirm "Для видео и картинок нужны ffmpeg и шрифты. Установить через apt?" y; then
                        apt_install ffmpeg fonts-dejavu-core >/dev/null || warn "ffmpeg не поставился — видео и мультики работать не будут."
                    else warn "Без ffmpeg видео и мультики работать не будут."; fi ;;
            mac)    if command -v brew >/dev/null 2>&1 && confirm "Для видео нужен ffmpeg. Установить через brew?" y; then brew install ffmpeg
                    else warn "Без ffmpeg видео и мультики работать не будут."; fi ;;
            *)      warn "Поставьте ffmpeg вручную — он нужен для видео." ;;
        esac
    fi

    make_venv "$py" || die "Не удалось создать .venv (на Ubuntu: apt install python3-venv)."
    .venv/bin/pip install -q --upgrade pip || warn "pip не обновился — продолжаю со старым."
    say "Ставлю зависимости (1–3 минуты)"
    .venv/bin/pip install -q . || die "Зависимости не поставились (ошибка выше). Проверьте интернет и запустите ./install.sh снова."
    ok "Зависимости установлены"
    if confirm "Установить браузер для парсинга сайтов (≈300 МБ, нужен для части источников)?" n; then
        .venv/bin/patchright install chromium || warn "Браузер не поставился — источники на нём будут пропущены."
    fi

    install_cloudflared
    mkdir -p data
    .venv/bin/python -m leadhunter.config \
        || die "Бот не принимает настройки из .env (что именно — выше). Исправьте файл и запустите ./install.sh снова."
    write_service
}

install_cloudflared() {
    if command -v cloudflared >/dev/null 2>&1; then ok "cloudflared уже есть"; return; fi
    if [ -x bin/cloudflared ]; then ok "cloudflared уже в ./bin"; return; fi
    say "Ставлю cloudflared (бесплатный HTTPS-адрес для кабинета)"
    if [ "$OS" = mac ]; then
        if command -v brew >/dev/null 2>&1; then brew install cloudflared || warn "Не вышло — кабинет будет выключен."
        else warn "Поставьте cloudflared (brew install cloudflared) — без него кабинет не откроется."; fi
        return
    fi
    local arch; arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) warn "Архитектура $arch: поставьте cloudflared вручную — без него кабинет не откроется."; return ;;
    esac
    if ! ensure_curl; then warn "Нет curl — cloudflared не скачать, кабинет (Mini App) будет выключен."; return; fi
    mkdir -p bin
    if curl -fsSL -o bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"; then
        chmod +x bin/cloudflared; ok "cloudflared скачан в ./bin"
    else rm -f bin/cloudflared; warn "Не удалось скачать cloudflared — кабинет (Mini App) будет выключен."; fi
}

RUN_CMD=""
write_service() {
    RUN_CMD="cd '$SELF_DIR' && PATH='$SELF_DIR/bin':\$PATH .venv/bin/python -m leadhunter"
    if [ "$OS" != debian ] || [ "$CAN_ROOT" != 1 ]; then return 0; fi
    # systemctl бывает и там, где systemd не запущен (WSL, контейнер) — тогда службу не создаём
    if ! command -v systemctl >/dev/null 2>&1 || [ ! -d "$ROOT/run/systemd/system" ]; then
        warn "systemd здесь не работает (контейнер или WSL) — автозапуск не настраиваю."
        return 0
    fi
    confirm "Запускать бота автоматически при старте сервера (systemd)?" y || return 0
    local user unit="$ROOT/etc/systemd/system/leadhunter.service"
    user="$(id -un)"
    if ! as_root tee "$unit" >/dev/null <<UNIT
[Unit]
Description=LeadHunter
After=network-online.target
Wants=network-online.target

[Service]
User=$user
WorkingDirectory=$SELF_DIR
Environment=PATH=$SELF_DIR/bin:/usr/local/bin:/usr/bin:/bin
ExecStartPre=$SELF_DIR/.venv/bin/python -m leadhunter.config
ExecStart=$SELF_DIR/.venv/bin/python -m leadhunter
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    then warn "Не удалось записать службу systemd — запускайте бота вручную."; return 0; fi
    if as_root systemctl daemon-reload && as_root systemctl enable --now leadhunter; then
        STARTED=1
        ok "Служба запущена. Логи: journalctl -u leadhunter -f"
    else
        warn "Служба systemd не запустилась — запускайте бота вручную (команда ниже)."
    fi
}

# ---------- выбор режима ----------
say "LeadHunter — установка"
setup_env

if [ -n "${LEADHUNTER_ENV_ONLY:-}" ]; then ok "Только .env (LEADHUNTER_ENV_ONLY)"; exit 0; fi

if [ -n "${LEADHUNTER_NO_DOCKER:-}" ]; then
    run_python
elif docker_ready; then
    ok "Docker найден"
    run_docker
else
    if command -v docker >/dev/null 2>&1; then
        warn "Docker установлен, но не отвечает (не запущен или нет прав)."
        if [ "$OS" = mac ]; then warn "Откройте приложение Docker Desktop и запустите установщик снова."; fi
    elif [ "$OS" = debian ] && confirm "Docker не найден. Установить его официальным скриптом get.docker.com? (проще всего)" y; then
        install_docker || warn "Продолжаю без Docker."
    elif [ "$OS" = mac ]; then
        echo "Для Mac проще всего Docker Desktop: https://docker.com/products/docker-desktop"
    fi
    if docker_ready; then
        run_docker
    else
        confirm "Запустить без Docker (через Python)?" y || die "Установите Docker и запустите установщик снова."
        run_python
    fi
fi

if [ "$STARTED" = 1 ]; then
    say "Готово: бот запущен — напишите ему /start"
else
    say "Установка завершена, но бот ещё не запущен"
    echo "  Запустите его этой командой и не закрывайте окно терминала:"
    echo "    $RUN_CMD"
    echo "  Когда бот запустится, напишите ему /start."
fi
echo "  Дальше в боте: выберите нишу кнопкой → /setup (ключ ИИ) → кнопка «Кабинет»."
echo "  Обновление:   ./update.sh"
