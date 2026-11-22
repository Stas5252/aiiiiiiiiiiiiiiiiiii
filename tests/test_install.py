"""Установка и запуск: .env.example, разбор настроек, install.sh и update.sh.

Скрипты гоняются в «песочнице»: свой PATH с заглушками docker/curl/systemctl/apt-get и подменённый
корень (LEADHUNTER_ROOT) вместо настоящих /etc и /run. Сеть, Docker и systemd не трогаются.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from leadhunter.config import DEFAULT_PHRASES, Settings

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456789:" + "A" * 35  # фиктивный, формат как у @BotFather
ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
FIELDS = {name.upper() for name in Settings.model_fields}


# ---------- .env.example ----------

def example_lines():
    return (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()


def example_vars(include_commented=False):
    """Переменные из .env.example. Значение — как его увидит Docker Compose: всё после «=» целиком."""
    out = {}
    for line in example_lines():
        if include_commented and line.startswith("# "):
            line = line[2:]
        m = ENV_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


@pytest.fixture
def clean_env(monkeypatch):
    for name in FIELDS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def write_env(tmp_path, extra="", token=TOKEN):
    text = re.sub(r"(?m)^BOT_TOKEN=.*$", f"BOT_TOKEN={token}", (ROOT / ".env.example").read_text(encoding="utf-8"))
    path = tmp_path / ".env"
    path.write_text(text + "\n" + extra, encoding="utf-8")
    return path


def test_env_example_has_no_inline_comments():
    """№1: Compose передаёт «KEY=   # комментарий» как значение — комментарии только отдельной строкой."""
    bad = [line for line in example_lines() if ENV_LINE.match(line) and re.search(r"\s#", line)]
    assert not bad, bad


def test_env_example_lists_every_setting():
    """№33: всё, что читает Settings, описано в .env.example, и лишнего там нет."""
    names = set(example_vars(include_commented=True))
    assert FIELDS - names == set()
    assert names - FIELDS == set()


def test_env_example_copy_starts_in_python(tmp_path, clean_env):
    """№2: cp .env.example .env + фиктивный токен → Settings() создаётся."""
    s = Settings(_env_file=write_env(tmp_path))
    assert s.bot_token == TOKEN and s.owner_ids == [] and s.tg_api_id is None and s.proxy_url is None


def test_env_example_copy_starts_with_compose_style_env(clean_env):
    """№1: то же, когда значения приходят переменными окружения — так их отдаёт Compose (env_file)."""
    for k, v in example_vars().items():
        clean_env.setenv(k, v)
    clean_env.setenv("BOT_TOKEN", TOKEN)
    s = Settings(_env_file=None)
    assert s.owner_ids == [] and s.proxy_url is None and s.vk_token is None and s.gemini_model == ""


def test_env_example_matches_code_defaults(tmp_path, clean_env):
    """Копия образца не должна менять поведение: заполненные строки равны значениям по умолчанию в коде."""
    from_example = Settings(_env_file=write_env(tmp_path))
    defaults = Settings(bot_token=TOKEN, _env_file=None)
    diff = {k: (getattr(from_example, k), getattr(defaults, k)) for k in Settings.model_fields
            if getattr(from_example, k) != getattr(defaults, k)}
    assert diff == {}


@pytest.mark.skipif(not shutil.which("docker"), reason="нет docker")
def test_env_example_through_docker_compose(tmp_path, clean_env):
    """№1 на настоящем Compose: docker compose config (демон не нужен) → значения → Settings()."""
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("нет docker compose")
    shutil.copy(ROOT / "docker-compose.yml", tmp_path)
    write_env(tmp_path)
    res = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=tmp_path, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    env = json.loads(res.stdout)["services"]["leadhunter"]["environment"]
    assert not [k for k, v in env.items() if (v or "").lstrip().startswith("#")]
    for k, v in env.items():
        clean_env.setenv(k, v or "")
    assert Settings(_env_file=None).owner_ids == []


# ---------- разбор настроек ----------

def test_empty_values_do_not_break_start(tmp_path, clean_env):
    """Пустая строка у любой настройки = «не задано», а не падение."""
    path = tmp_path / ".env"
    path.write_text(f"BOT_TOKEN={TOKEN}\n" + "".join(f"{n}=\n" for n in sorted(FIELDS - {"BOT_TOKEN"})), encoding="utf-8")
    s = Settings(_env_file=path)
    assert s.tg_api_id is None and s.kwork_enabled is True and s.score_threshold == 5
    assert s.db_path == "data/leads.db" and s.digest_tz == "Europe/Moscow"
    assert s.digest_hour is None  # пусто = дайджест выключен
    assert s.rss_feeds == [] and s.search_phrases == []  # пустой список = источник выключен, как и раньше
    assert Settings(bot_token=TOKEN, _env_file=None).search_phrases == DEFAULT_PHRASES


def test_russian_yes_no(clean_env):
    clean_env.setenv("KWORK_ENABLED", "нет")
    clean_env.setenv("DELIVER_DM", "да")
    s = Settings(bot_token=TOKEN, _env_file=None)
    assert s.kwork_enabled is False and s.deliver_dm is True


@pytest.mark.parametrize("name, value, expect", [
    ("SCORE_THRESHOLD", "высокий", "целое число"),
    ("KWORK_ENABLED", "maybe", "true или false"),
    ("OWNER_IDS", "@stas", "через запятую"),
    ("DIGEST_HOUR", "24", "от 0 до 23"),
    ("CONTENT_HOUR", "-1", "от 0 до 23"),
    ("DIGEST_TZ", "Moscow", "часовой пояс"),
    ("WORK_HOURS", "с 8 до 23", "8-23"),
    ("KWORK_INTERVAL", "0", "больше нуля"),
    ("MINIAPP_PORT", "99999", "порт"),
    ("MINIAPP_TUNNEL", "ngrok", "cloudflare"),
    ("TG_API_ID", "abc", "целое число"),
])
def test_wrong_value_gives_clear_russian_message(clean_env, name, value, expect):
    """Неверное значение → понятное сообщение на русском с именем переменной (№21 из leads: DIGEST_*)."""
    from leadhunter.config import ConfigError
    clean_env.setenv(name, value)
    with pytest.raises(ConfigError) as err:
        Settings(bot_token=TOKEN, _env_file=None)
    text = str(err.value)
    assert name in text and expect in text and ".env" in text
    assert "validation error" not in text and "pydantic" not in text


@pytest.mark.parametrize("token, expect", [(None, "не задан"), ("", "не задан"), ("просто текст", "BotFather"),
                                           ("123456:ABC-DEF", "пример")])
def test_bad_bot_token_message_hides_value(clean_env, token, expect):
    from leadhunter.config import ConfigError
    if token is not None:
        clean_env.setenv("BOT_TOKEN", token)
    with pytest.raises(ConfigError) as err:
        Settings(_env_file=None)
    assert "BOT_TOKEN" in str(err.value) and expect in str(err.value)
    assert not token or token not in str(err.value)


def test_secret_values_are_not_printed(clean_env):
    from leadhunter.config import ConfigError
    clean_env.setenv("TG_API_HASH", "s3cr3t-hash")
    clean_env.setenv("SLA_MINUTES", "десять")
    with pytest.raises(ConfigError) as err:
        Settings(bot_token=TOKEN, _env_file=None)
    assert "десять" in str(err.value) and "s3cr3t" not in str(err.value) and TOKEN not in str(err.value)


def test_all_errors_listed_at_once(clean_env):
    from leadhunter.config import ConfigError
    clean_env.setenv("DIGEST_HOUR", "25")
    clean_env.setenv("HOT_SCORE", "x")
    with pytest.raises(ConfigError) as err:
        Settings(bot_token=TOKEN, _env_file=None)
    assert "DIGEST_HOUR" in str(err.value) and "HOT_SCORE" in str(err.value)


def test_hh_is_off_by_default(clean_env):
    """leads №5: hh.ru без своего User-Agent отвечает 400 и шлёт владельцу алерты — по умолчанию выключен."""
    assert Settings(bot_token=TOKEN, _env_file=None).hh_enabled is False


def test_hh_needs_real_contact(clean_env, caplog):
    clean_env.setenv("HH_ENABLED", "true")
    for ua in ("", "LeadHunter/0.1 (you@example.com)", "LeadHunter/0.1"):
        clean_env.setenv("HH_USER_AGENT", ua)
        with caplog.at_level(logging.WARNING, logger="leadhunter.config"):
            assert Settings(bot_token=TOKEN, _env_file=None).hh_enabled is False
        assert "HH_USER_AGENT" in caplog.text
        caplog.clear()
    clean_env.setenv("HH_USER_AGENT", "LeadHunter/0.1 (stas@studio.ru)")
    assert Settings(bot_token=TOKEN, _env_file=None).hh_enabled is True


def run_py(args, cwd, env=None):
    base = {k: v for k, v in os.environ.items() if k not in FIELDS}
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True, env={**base, **(env or {})})


def test_config_self_check_command(tmp_path):
    """python -m leadhunter.config — проверка настроек, её вызывают install.sh и update.sh перед запуском."""
    write_env(tmp_path)
    ok = run_py(["-m", "leadhunter.config"], tmp_path, {"PYTHONPATH": str(ROOT)})
    assert ok.returncode == 0 and "в порядке" in ok.stdout, ok.stderr
    write_env(tmp_path, "DIGEST_TZ=Moscow\n")
    bad = run_py(["-m", "leadhunter.config"], tmp_path, {"PYTHONPATH": str(ROOT)})
    assert bad.returncode == 1 and "DIGEST_TZ" in bad.stderr and "Traceback" not in bad.stderr


def test_bot_start_with_bad_env_names_the_setting(tmp_path):
    """python -m leadhunter с ошибкой в .env не стартует и называет настройку по-русски.
    Контейнер и служба systemd перед ботом запускают проверку настроек — там сообщение без трассы Python."""
    write_env(tmp_path, "SCORE_THRESHOLD=много\n")
    res = run_py(["-m", "leadhunter"], tmp_path, {"PYTHONPATH": str(ROOT)})
    assert res.returncode == 1 and "SCORE_THRESHOLD: нужно целое число" in res.stderr
    assert "python -m leadhunter.config && exec python -m leadhunter" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ExecStartPre=$SELF_DIR/.venv/bin/python -m leadhunter.config" in (ROOT / "install.sh").read_text(encoding="utf-8")


# ---------- файлы репозитория ----------

def test_gitignore_and_gitattributes():
    """№33, №5: bin/ и build/ после установки не светятся в git status; .sh всегда с LF."""
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert "bin/" in ignored and "build/" in ignored
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\*\.sh\s+text\s+eol=lf\s*$", attrs)


@pytest.mark.parametrize("script", ["install.sh", "update.sh"])
def test_scripts_are_lf_and_executable(script):
    path = ROOT / script
    assert b"\r" not in path.read_bytes()
    assert os.access(path, os.X_OK)
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="нет shellcheck")
def test_shellcheck_clean():
    res = subprocess.run(["shellcheck", "install.sh", "update.sh"], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_readme_matches_code():
    """README: каждая настройка названа, команды установки ведут на этот репозиторий, переменные установщика описаны."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert FIELDS - set(re.findall(r"`([A-Z][A-Z0-9_]+)`", readme)) == set()
    mentioned = set(re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", readme))
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    installer_vars = set(re.findall(r"\bLEADHUNTER_[A-Z_]+\b", install)) - {"LEADHUNTER_ROOT"}
    unknown = {n for n in mentioned - FIELDS - installer_vars if not n.startswith(("DEBIAN_", "PYTHON"))}
    assert unknown == set()
    assert installer_vars - mentioned == set()
    repo = re.search(r"github\.com/([\w.-]+/[\w.-]+?)\.git", install).group(1)
    assert f"raw.githubusercontent.com/{repo}/main/install.sh" in readme
    assert "≈ 3 с" not in readme  # №31: на 2 vCPU 20 секунд мультика рендерятся около 5 с


# ---------- песочница для install.sh / update.sh ----------

REAL_TOOLS = ["bash", "sh", "awk", "sed", "grep", "cat", "mktemp", "chmod", "rm", "head", "cut", "dirname", "mkdir",
              "tee", "tr", "cp", "mv", "ls", "touch", "sleep", "env", "sort", "wc", "basename", "git", "true", "false"]

STUB_HEAD = '#!/bin/bash\necho "$(basename "$0") $*" >> "$SANDBOX/calls.log"\n'

STUBS = {
    "uname": 'case "$1" in -m) echo x86_64 ;; *) echo Linux ;; esac\n',
    "id": 'case "$1" in -u) echo "${FAKE_UID:-0}" ;; -un) echo tester ;; *) echo "uid=${FAKE_UID:-0}" ;; esac\n',
    "apt-get": ('[ "${FAKE_UID:-0}" = 0 ] || [ -n "$VIA_SUDO" ] || exit 100\n'
                'for a in "$@"; do [ "$a" = curl ] && cp "$SANDBOX/stubs/curl" "$SANDBOX/bin/curl"; done\nexit 0\n'),
    "sudo": 'VIA_SUDO=1 exec "$@"\n',
    "dpkg": "exit 0\n",
    "ffmpeg": "exit 0\n",
    "cloudflared": "exit 0\n",
    "python3": ('case "$1" in\n'
                '  -c) exit 0 ;;\n'
                '  --version) echo "Python 3.12.3" ;;\n'
                '  -m) mkdir -p "$3/bin"; for t in pip python patchright; do cp "$SANDBOX/stubs/venvtool" "$3/bin/$t"; done ;;\n'
                'esac\n'),
    "venvtool": 'if [ "$(basename "$0")" = python ] && [ "$*" = "-m leadhunter.config" ]; then exit "${FAKE_CONFIG_RC:-0}"; fi\n',
    "curl": ('out=""; url=""; prev=""\n'
             'for a in "$@"; do [ "$prev" = -o ] && out="$a"; case "$a" in http*) url="$a" ;; esac; prev="$a"; done\n'
             'case "$url" in *get.docker.com*) [ -n "$FAKE_DOCKER_DL_FAIL" ] && exit 22 ;; esac\n'
             'body="exit ${FAKE_DOCKER_SH_RC:-0}"\n'
             'if [ -n "$out" ]; then echo "$body" > "$out"; else echo "$body"; fi\n'),
    "docker": ('case "$1 $2" in\n'
               '  "info "*) exit "${FAKE_DOCKER_INFO_RC:-0}" ;;\n'
               '  "compose ps") echo "${FAKE_COMPOSE_PS:-}" ;;\n'
               '  "compose run") exit "${FAKE_CONFIG_RC:-0}" ;;\n'
               'esac\nexit 0\n'),
    "systemctl": ('case "$1" in\n'
                  '  cat) [ -n "$FAKE_UNIT_EXISTS" ] || exit 1 ;;\n'
                  '  enable|daemon-reload|restart) exit "${FAKE_SYSTEMCTL_RC:-0}" ;;\n'
                  'esac\nexit 0\n'),
}


class Sandbox:
    def __init__(self, tmp):
        self.tmp = tmp
        self.bin, self.stubs = tmp / "bin", tmp / "stubs"
        self.root, self.proj = tmp / "root", tmp / "proj"
        for d in (self.bin, self.stubs, self.root / "etc/systemd/system", self.proj / "leadhunter"):
            d.mkdir(parents=True)
        (self.root / "etc/os-release").write_text("ID=ubuntu\nID_LIKE=debian\n")
        for name in ("install.sh", "update.sh", ".env.example", "pyproject.toml", "docker-compose.yml"):
            shutil.copy(ROOT / name, self.proj)
        for tool in REAL_TOOLS:
            if shutil.which(tool):
                (self.bin / tool).symlink_to(shutil.which(tool))
        for name, body in STUBS.items():
            path = self.stubs / name
            path.write_text(STUB_HEAD + body)
            path.chmod(0o755)

    def have(self, *names):
        for name in names:
            shutil.copy(self.stubs / name, self.bin / name)
        return self

    def systemd(self):
        (self.root / "run/systemd/system").mkdir(parents=True)
        return self.have("systemctl")

    def run(self, script="install.sh", cwd=None, **env):
        full = {"PATH": str(self.bin), "HOME": str(self.tmp), "SANDBOX": str(self.tmp), "LEADHUNTER_ROOT": str(self.root),
                "LEADHUNTER_YES": "1", "LEADHUNTER_SKIP_NET": "1", "BOT_TOKEN": TOKEN, "LC_ALL": "C",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **env}
        res = subprocess.run([str(self.bin / "bash"), str((cwd or self.proj) / script)], cwd=cwd or self.proj,
                             capture_output=True, text=True, env=full, timeout=120)
        res.text = res.stdout + res.stderr
        return res

    @property
    def calls(self):
        log = self.tmp / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def called(self, prefix):
        return [c for c in self.calls if c.startswith(prefix)]


@pytest.fixture
def box(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("песочница установщика рассчитана на Linux")
    return Sandbox(tmp_path).have("uname", "id", "apt-get", "dpkg", "ffmpeg", "cloudflared", "python3")


# ---------- install.sh ----------

def test_install_env_only_writes_startable_env(box, clean_env):
    """После установщика .env читается ботом (№2) и не содержит комментариев в значениях (№1)."""
    res = box.run(LEADHUNTER_ENV_ONLY="1", GEMINI_API_KEY="gem-key")
    assert res.returncode == 0, res.text
    assert TOKEN not in res.text and "gem-key" not in res.text
    s = Settings(_env_file=box.proj / ".env")
    assert s.bot_token == TOKEN and s.gemini_api_key == "gem-key" and s.miniapp_tunnel == "cloudflare"
    assert oct((box.proj / ".env").stat().st_mode & 0o777) == "0o600"


def test_install_repairs_old_env_with_inline_comments(box, clean_env):
    """.env, сделанный из старого образца: «OWNER_IDS=   # комментарий» → Compose больше не получит комментарий."""
    (box.proj / ".env").write_text(f"BOT_TOKEN={TOKEN}\nOWNER_IDS=                # ваш Telegram id\n"
                                   "PROXY_URL=   # http://user:pass@host:port\nMINIAPP_URL=https://x.example/app\n"
                                   "PROFILE=Сайты #1 в городе\n", encoding="utf-8")
    assert box.run(LEADHUNTER_ENV_ONLY="1").returncode == 0
    text = (box.proj / ".env").read_text(encoding="utf-8")
    assert "OWNER_IDS=\n" in text and "PROXY_URL=\n" in text and "PROFILE=Сайты #1 в городе\n" in text
    assert "MINIAPP_TUNNEL" not in text  # свой адрес кабинета задан — туннель не навязываем


def test_install_without_curl_as_root_in_container(box):
    """№6, №7, №33: чистая Ubuntu — нет curl, Docker не встаёт, systemd не запущен. Установка доходит до конца."""
    res = box.have("systemctl").run()
    assert res.returncode == 0, res.text
    assert box.called("apt-get install") and "curl" in " ".join(box.called("apt-get install"))
    assert box.called("curl") and "get.docker.com" in " ".join(box.called("curl"))
    assert (box.proj / ".venv/bin/pip").exists() and box.called("pip install")
    assert not box.called("systemctl")  # systemd не запущен — службу не трогаем
    assert ".venv/bin/python -m leadhunter" in res.text and "ещё не запущен" in res.text
    assert "Готово: напишите боту /start" not in res.text


def test_install_survives_docker_download_failure(box):
    """№6: get.docker.com недоступен — не падаем, ставим через Python."""
    res = box.have("curl").run(FAKE_DOCKER_DL_FAIL="1")
    assert res.returncode == 0, res.text
    assert box.called("pip install") and "ещё не запущен" in res.text


def test_install_survives_docker_script_failure(box):
    res = box.have("curl").run(FAKE_DOCKER_SH_RC="1")
    assert res.returncode == 0, res.text
    assert box.called("pip install")


def test_install_no_docker_flag(box):
    """№6: LEADHUNTER_NO_DOCKER=1 — Python-режим без вопросов, даже если Docker есть."""
    res = box.have("curl", "docker").run(LEADHUNTER_NO_DOCKER="1")
    assert res.returncode == 0, res.text
    assert not box.called("docker") and not [c for c in box.called("curl") if "get.docker.com" in c]
    assert box.called("pip install")


def test_install_docker_mode(box):
    res = box.have("curl", "docker").run()
    assert res.returncode == 0, res.text
    assert box.called("docker compose run") and box.called("docker compose up -d")
    assert not box.called("pip") and "бот запущен" in res.text


def test_install_docker_mode_stops_on_bad_settings(box):
    """Ошибка в .env ловится до запуска контейнера, а не циклом перезапусков."""
    res = box.have("curl", "docker").run(FAKE_CONFIG_RC="1")
    assert res.returncode != 0 and not box.called("docker compose up") and ".env" in res.text


def test_install_python_mode_stops_on_bad_settings(box):
    res = box.systemd().run(LEADHUNTER_NO_DOCKER="1", FAKE_CONFIG_RC="1")
    assert res.returncode != 0 and not box.called("systemctl") and ".env" in res.text


def test_install_systemd_service(box):
    res = box.systemd().run(LEADHUNTER_NO_DOCKER="1")
    assert res.returncode == 0, res.text
    unit = (box.root / "etc/systemd/system/leadhunter.service").read_text()
    assert f"WorkingDirectory={box.proj}" in unit and "-m leadhunter" in unit and "User=tester" in unit
    assert box.called("systemctl daemon-reload") and box.called("systemctl enable --now leadhunter")
    assert "бот запущен" in res.text and "ещё не запущен" not in res.text


def test_install_systemd_start_failure_is_not_fatal(box):
    """№7: systemctl есть, но службу поднять не удалось — печатаем команду ручного запуска."""
    res = box.systemd().run(LEADHUNTER_NO_DOCKER="1", FAKE_SYSTEMCTL_RC="1")
    assert res.returncode == 0, res.text
    assert ".venv/bin/python -m leadhunter" in res.text and "ещё не запущен" in res.text


def test_install_non_root_without_sudo_explains(box):
    """Нет прав и нет sudo: понятная просьба, а не «apt-get: Permission denied»."""
    (box.bin / "ffmpeg").unlink()
    res = box.run(LEADHUNTER_NO_DOCKER="1", FAKE_UID="1000")
    assert "root" in res.text and ("ffmpeg" in res.text)
    assert not box.called("apt-get")


def test_install_non_root_uses_sudo(box):
    (box.bin / "ffmpeg").unlink()
    res = box.have("sudo").run(LEADHUNTER_NO_DOCKER="1", FAKE_UID="1000")
    assert res.returncode == 0, res.text
    assert box.called("sudo env DEBIAN_FRONTEND=noninteractive apt-get install")


# ---------- update.sh ----------

def git(cwd, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture
def repo(box):
    """«Сервер» с двумя версиями и установленная копия на первой версии (как после git clone + install.sh)."""
    up, inst = box.tmp / "upstream", box.tmp / "installed"
    up.mkdir()
    shutil.copy(ROOT / "update.sh", up)
    (up / "version.txt").write_text("1\n")
    git(up, "init", "-q")
    git(up, "add", "-A")
    git(up, "commit", "-qm", "v1")
    git(box.tmp, "clone", "-q", str(up), str(inst))
    (up / "version.txt").write_text("2\n")
    git(up, "commit", "-qam", "v2")
    (inst / ".venv/bin").mkdir(parents=True)
    for tool in ("pip", "python"):
        shutil.copy(box.stubs / "venvtool", inst / ".venv/bin" / tool)
    box.inst, box.up = inst, up
    return box


def test_update_root_without_sudo_restarts_service(repo):
    """№8: root на VPS без sudo — служба перезапускается, exit 0."""
    res = repo.have("systemctl").run("update.sh", cwd=repo.inst, FAKE_UNIT_EXISTS="1")
    assert res.returncode == 0, res.text
    assert (repo.inst / "version.txt").read_text() == "2\n"
    assert repo.called("pip install") and repo.called("systemctl restart leadhunter")


def test_update_non_root_restarts_through_sudo(repo):
    res = repo.have("systemctl", "sudo").run("update.sh", cwd=repo.inst, FAKE_UNIT_EXISTS="1", FAKE_UID="1000")
    assert res.returncode == 0, res.text
    assert repo.called("sudo systemctl restart leadhunter")


def test_update_without_service_tells_how_to_restart(repo):
    res = repo.run("update.sh", cwd=repo.inst)
    assert res.returncode == 0 and ".venv/bin/python -m leadhunter" in res.text


def test_update_docker_mode(repo):
    res = repo.have("docker").run("update.sh", cwd=repo.inst, FAKE_COMPOSE_PS="abc123")
    assert res.returncode == 0, res.text
    assert repo.called("docker compose up -d") and not repo.called("pip")


def test_update_stops_before_restart_on_bad_settings(repo):
    """Новая версия не принимает .env — говорим об этом и не роняем работающего бота перезапуском."""
    res = repo.have("systemctl").run("update.sh", cwd=repo.inst, FAKE_UNIT_EXISTS="1", FAKE_CONFIG_RC="1")
    assert res.returncode != 0 and not repo.called("systemctl restart") and ".env" in res.text


def test_update_not_a_git_folder(box):
    """№32: папка из архива — так и говорим, а не «в папке есть ваши правки»."""
    res = box.run("update.sh")
    assert res.returncode != 0 and "git clone" in res.text and "правки" not in res.text


def test_update_no_network(repo):
    """№32: сервер недоступен — сообщение про связь, а не про правки."""
    git(repo.inst, "remote", "set-url", "origin", str(repo.tmp / "nowhere"))
    res = repo.run("update.sh", cwd=repo.inst)
    assert res.returncode != 0 and "интернет" in res.text and "правки" not in res.text
    assert not repo.called("pip")


def test_update_local_changes(repo):
    (repo.inst / "version.txt").write_text("моя правка\n")
    res = repo.run("update.sh", cwd=repo.inst)
    assert res.returncode != 0 and "правки" in res.text and "version.txt" in res.text and "git stash" in res.text
    assert not repo.called("pip")


def test_update_local_commits(repo):
    (repo.inst / "mine.txt").write_text("x\n")
    git(repo.inst, "add", "-A")
    git(repo.inst, "commit", "-qm", "mine")
    res = repo.run("update.sh", cwd=repo.inst)
    assert res.returncode != 0 and "коммит" in res.text and not repo.called("pip")


def test_update_repairs_old_env(repo):
    (repo.inst / ".env").write_text(f"BOT_TOKEN={TOKEN}\nOWNER_IDS=      # ваш Telegram id\n", encoding="utf-8")
    assert repo.run("update.sh", cwd=repo.inst).returncode == 0
    assert "OWNER_IDS=\n" in (repo.inst / ".env").read_text(encoding="utf-8")
