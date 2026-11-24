import pytest

from leadhunter import features


@pytest.fixture(autouse=True)
def extra_features_on(request, monkeypatch):
    """Мультики, ИИ-видео, лид-магниты, VK и YouTube в продукте выключены по умолчанию, но их код остаётся —
    старые тесты проверяют его с включёнными переключателями. Поведение по умолчанию — тесты с меткой product_defaults."""
    if request.node.get_closest_marker("product_defaults"):
        return
    monkeypatch.setattr(features, "DEFAULTS", {**features.DEFAULTS, **{key: "1" for key in features.EXTRA}})
