"""Тесты не читают настройки из окружения машины, на которой запущены."""
import os

from leadhunter.config import Settings

# Выставляется при сборе тестов — раньше любой фикстуры, как переменная, заданная в оболочке.
os.environ.setdefault("SERPER_API_KEY", "из-оболочки")


def test_settings_ignore_shell_environment():
    assert "SERPER_API_KEY" not in os.environ
    assert Settings(bot_token="123456:FAKE", _env_file=None).serper_api_key is None


def test_settings_still_read_env_set_by_the_test(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "из-теста")
    assert Settings(bot_token="123456:FAKE", _env_file=None).serper_api_key == "из-теста"
