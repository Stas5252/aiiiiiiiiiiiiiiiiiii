"""Подключения «для чайников»: ключи API вводятся в боте (/setup) или в кабинете, хранятся в базе,
проверяются живым запросом и применяются без перезапуска. .env остаётся для продвинутых."""
import logging
from dataclasses import dataclass

import httpx

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class Integration:
    key: str  # поле Settings
    title: str
    group: str  # ai | publish | sources
    hint: str  # где взять, одной строкой
    url: str = ""
    secret: bool = True
    requires: tuple[str, ...] = ()  # другие поля, без которых не работает


INTEGRATIONS: dict[str, Integration] = {i.key: i for i in [
    Integration("gemini_api_key", "Gemini (ИИ: тексты, озвучка, картинки, видео)", "ai",
                "aistudio.google.com → Get API key. Бесплатный лимит есть на тексты и озвучку.", "https://aistudio.google.com/apikey"),
    Integration("openai_api_key", "OpenAI / OpenRouter / DeepSeek (ИИ-тексты)", "ai",
                "platform.openai.com → API keys. Подписка ChatGPT Plus не подходит.", "https://platform.openai.com/api-keys"),
    Integration("vk_token", "VK (поиск постов + публикация)", "publish",
                "Токен сообщества: управление сообществом → Работа с API → Создать ключ (права: стена, сообщения). Для фото/видео нужен токен пользователя.",
                "https://dev.vk.com/ru/api/access-token/getting-started"),
    Integration("threads_access_token", "Threads — токен", "publish",
                "developers.facebook.com → приложение Threads → Use cases → Access token (права threads_basic, threads_content_publish).",
                "https://developers.facebook.com/docs/threads/get-started"),
    Integration("threads_user_id", "Threads — ID пользователя", "publish", "GET graph.threads.net/v1.0/me — поле id.", secret=False,
                requires=("threads_access_token",)),
    Integration("ig_access_token", "Instagram — токен", "publish",
                "Бизнес-аккаунт + приложение Meta, права instagram_content_publish.", "https://developers.facebook.com/docs/instagram-platform"),
    Integration("ig_user_id", "Instagram — ID аккаунта", "publish", "Числовой ID бизнес-аккаунта Instagram.", secret=False, requires=("ig_access_token",)),
    Integration("yt_client_id", "YouTube — Client ID", "publish", "Google Cloud → OAuth client (Desktop).", "https://console.cloud.google.com/apis/credentials", secret=False),
    Integration("yt_client_secret", "YouTube — Client secret", "publish", "Из того же OAuth-клиента.", requires=("yt_client_id",)),
    Integration("yt_refresh_token", "YouTube — Refresh token", "publish",
                "Один раз пройти OAuth с правом youtube.upload (например, через OAuth Playground) и взять refresh_token.",
                "https://developers.google.com/oauthplayground", requires=("yt_client_id", "yt_client_secret")),
    Integration("serper_api_key", "Serper (поиск по Threads/vc.ru через Google)", "sources", "serper.dev — 2500 запросов бесплатно.", "https://serper.dev"),
    Integration("proxy_url", "Прокси для парсинга (http://user:pass@host:port)", "sources", "Резидентный или мобильный прокси, если пошли капчи.", secret=False),
    Integration("yandex_geosearch_key", "Яндекс Geosearch API", "sources", "developer.tech.yandex.ru — бесплатно 500 запросов в день.", "https://developer.tech.yandex.ru"),
]}

DB_PREFIX = "secret:"


def mask(value: str | None) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-3:] if len(value) > 10 else "•" * len(value)


async def load_overrides(db, s: Settings) -> list[str]:
    """Значения из базы перекрывают пустые поля .env. Возвращает список применённых ключей."""
    applied = []
    for key in INTEGRATIONS:
        if getattr(s, key, None):
            continue
        v = await db.get_setting(DB_PREFIX + key)
        if v:
            setattr(s, key, v)
            applied.append(key)
    return applied


async def validate(key: str, value: str, s: Settings) -> str | None:
    """Живая проверка ключа. None = ок, иначе текст ошибки для человека."""
    value = value.strip()
    if not value:
        return "пустое значение"
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            if key == "gemini_api_key":
                r = await c.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", headers={"x-goog-api-key": value})
                if r.status_code != 200:
                    return f"Gemini не принял ключ (HTTP {r.status_code}): {_msg(r)}"
            elif key == "openai_api_key":
                r = await c.get(s.openai_base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {value}"})
                if r.status_code != 200:
                    return f"API не принял ключ (HTTP {r.status_code}): {_msg(r)}"
            elif key == "vk_token":
                r = await c.post("https://api.vk.ru/method/utils.getServerTime", data={"access_token": value, "v": "5.199"})
                if "error" in r.json():
                    return f"VK: {r.json()['error'].get('error_msg')}"
            elif key == "threads_access_token":
                r = await c.get("https://graph.threads.net/v1.0/me", params={"fields": "id,username", "access_token": value})
                if r.status_code != 200:
                    return f"Threads: {_msg(r)}"
            elif key == "ig_access_token":
                r = await c.get("https://graph.facebook.com/v21.0/me", params={"access_token": value})
                if r.status_code != 200:
                    return f"Instagram/Meta: {_msg(r)}"
            elif key == "yt_refresh_token":
                if not (s.yt_client_id and s.yt_client_secret):
                    return "сначала задайте YouTube Client ID и Client secret"
                r = await c.post("https://oauth2.googleapis.com/token", data={"client_id": s.yt_client_id, "client_secret": s.yt_client_secret,
                                                                            "refresh_token": value, "grant_type": "refresh_token"})
                if r.status_code != 200:
                    return f"Google OAuth: {_msg(r)}"
            elif key == "serper_api_key":
                r = await c.post("https://google.serper.dev/search", headers={"X-API-KEY": value}, json={"q": "test", "num": 1})
                if r.status_code != 200:
                    return f"Serper: HTTP {r.status_code}"
            elif key == "proxy_url":
                if not value.startswith(("http://", "https://", "socks5://")):
                    return "формат: http://user:pass@host:port"
    except httpx.HTTPError as e:
        return f"сеть: {e}"
    return None


def _msg(r: httpx.Response) -> str:
    try:
        j = r.json()
        return str((j.get("error") or {}).get("message") or j.get("error_description") or j)[:200]
    except ValueError:
        return r.text[:200]


class Integrations:
    """Статус подключений + сохранение с горячим применением."""

    def __init__(self, db, s: Settings, on_change=None):
        self.db = db
        self.s = s
        self.on_change = on_change  # async () -> None: пересобрать ИИ/медиа после смены ключей

    async def status(self) -> list[dict]:
        out = []
        for i in INTEGRATIONS.values():
            val = getattr(self.s, i.key, None)
            from_db = bool(await self.db.get_setting(DB_PREFIX + i.key))
            out.append({"key": i.key, "title": i.title, "group": i.group, "hint": i.hint, "url": i.url, "secret": i.secret,
                        "set": bool(val), "masked": mask(val) if i.secret else (val or ""), "editable": from_db or not val,
                        "requires": list(i.requires)})
        return out

    async def set(self, key: str, value: str) -> str | None:
        if key not in INTEGRATIONS:
            return "неизвестное подключение"
        err = await validate(key, value, self.s)
        if err:
            return err
        await self.db.set_setting(DB_PREFIX + key, value.strip())
        setattr(self.s, key, value.strip())
        if self.on_change:
            await self.on_change()
        return None

    async def unset(self, key: str) -> None:
        await self.db.set_setting(DB_PREFIX + key, "")
        setattr(self.s, key, None if INTEGRATIONS[key].secret or key.endswith("_id") else "")
        if self.on_change:
            await self.on_change()

    def summary(self) -> dict[str, bool]:
        s = self.s
        return {
            "ai": bool(s.gemini_api_key or s.openai_api_key),
            "media_ai": bool(s.gemini_api_key),
            "vk": bool(s.vk_token),
            "threads": bool(s.threads_access_token and s.threads_user_id),
            "instagram": bool(s.ig_access_token and s.ig_user_id),
            "youtube": bool(s.yt_client_id and s.yt_client_secret and s.yt_refresh_token),
            "tg_account": bool(s.tg_api_id and s.tg_api_hash),
            "miniapp": bool(s.miniapp_url),
        }
