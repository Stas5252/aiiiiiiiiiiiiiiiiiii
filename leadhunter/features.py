"""Переключатели функций и их значения по умолчанию — одно место для бота, кабинета и контент-фабрики.

Продукт — поиск лидов и ведение Threads и Instagram. Остальное (EXTRA) выключено по умолчанию
и убрано из меню, но код остаётся: включается в кабинете → Настройки → «Дополнительно».
"""
from .models import SOURCE_SETTINGS

EXTRA = ("format_toon", "format_aivideo", "format_magnet", "publish_vk", "publish_youtube")
EXTRA_HINT = ("Мультики, ИИ-видео, лид-магниты, VK и YouTube по умолчанию выключены — "
              "управление в кабинете: Настройки → «Дополнительно».")

DEFAULTS = {
    "paused": "0", "autopilot": "0", "schedule_enabled": "1",
    "format_post": "1", "format_carousel": "1", "format_reel": "1", "format_ideas": "1",
    "publish_telegram": "1", "publish_instagram": "1", "publish_threads": "1",
    **{key: "0" for key in EXTRA},
    **dict(SOURCE_SETTINGS.values()),
}


async def enabled(db, key: str) -> bool:
    """Неизвестный переключатель считается включённым — как было до появления этого модуля."""
    return await db.get_setting(key, DEFAULTS.get(key, "1")) != "0"
