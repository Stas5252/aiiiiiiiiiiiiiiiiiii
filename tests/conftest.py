import pytest

from leadhunter import features


@pytest.fixture(autouse=True)
def extra_features_on(request, monkeypatch):
    """Мультики, ИИ-видео, лид-магниты, VK и YouTube в продукте выключены по умолчанию, но их код остаётся —
    старые тесты проверяют его с включёнными переключателями. Поведение по умолчанию — тесты с меткой product_defaults."""
    if request.node.get_closest_marker("product_defaults"):
        return
    monkeypatch.setattr(features, "DEFAULTS", {**features.DEFAULTS, **{key: "1" for key in features.EXTRA}})


@pytest.fixture(autouse=True)
def no_settings_from_shell(monkeypatch):
    """Настройки из окружения машины (ключи ИИ, токены, прокси) в тесты не попадают:
    иначе тест молча получит настоящий ключ, а упавшая проверка напечатает его в отчёт."""
    from leadhunter.config import Settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
