import logging
import re
import sys
from typing import Annotated, get_args, get_origin
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .cities import SITE_RUBRICS

log = logging.getLogger(__name__)

# Списки в .env задаются через запятую: OWNER_IDS=123,456
StrList = Annotated[list[str], NoDecode]
IntList = Annotated[list[int], NoDecode]

DEFAULT_PROFILE = (
    "Студия делает: сайты и лендинги под ключ, Telegram-боты, ИИ-автоматизацию бизнеса "
    "(чат-боты, интеграции, CRM, n8n), ИИ-мультики и видеоролики. Работаем удалённо по всей России и СНГ."
)

DEFAULT_PHRASES = [
    "нужен сайт", "ищу кто сделает сайт", "нужен telegram бот", "ищу разработчика бота",
    "нужна автоматизация", "ищу кто сделает мультик", "нужен ролик нейросеть", "посоветуйте кто делает сайты",
    "нужен лендинг", "ищу веб разработчика", "кто делает чат ботов", "нужна нейросеть для бизнеса",
]

# Значения этих настроек никогда не показываем в сообщениях об ошибках
SECRET_PARTS = ("token", "key", "secret", "hash", "proxy_url", "webhook_url")
PLACEHOLDER_TOKEN = "123456:ABC-DEF"  # пример из .env.example
YES = {"да", "вкл", "включено"}
NO = {"нет", "выкл", "выключено"}


class ConfigError(ValueError):
    """Ошибка в настройках (.env): текст для человека, на русском, без значений секретов."""


def _explain(err: ValidationError) -> str:
    kinds = {
        "missing": "не задан — это обязательная настройка",
        "int_parsing": "нужно целое число",
        "int_type": "нужно целое число",
        "int_from_float": "нужно целое число",
        "bool_parsing": "нужно true или false (можно «да» или «нет»)",
        "bool_type": "нужно true или false (можно «да» или «нет»)",
        "string_type": "нужен текст",
    }
    lines = []
    for e in err.errors():
        field = str(e["loc"][0]) if e["loc"] else ""
        if e["type"] == "value_error":
            what = str(e.get("ctx", {}).get("error") or e["msg"])
        else:
            what = kinds.get(e["type"]) or f"неверное значение ({e['msg']})"
        shown = ""
        value = e.get("input")
        if field and e["type"] != "missing" and isinstance(value, (str, int, float)) and not isinstance(value, bool) \
                and str(value).strip() and not any(part in field for part in SECRET_PARTS):
            shown = f" (сейчас: «{str(value)[:60]}»)"
        lines.append(f"  • {field.upper()}: {what}{shown}" if field else f"  • {what}")
    return ("В настройках ошибка — бот не запущен.\n" + "\n".join(lines)
            + "\nОткройте файл .env, исправьте эти строки и запустите снова. Образец с пояснениями — .env.example.")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    owner_ids: IntList = Field(default_factory=list)
    db_path: str = "data/leads.db"
    score_threshold: int = 5
    max_age_hours: int = 48  # старше — не лид, уже разобрали
    profile: str = DEFAULT_PROFILE  # что продаём — идёт в промпт ИИ; меняется через /profile
    search_phrases: StrList = Field(default_factory=lambda: list(DEFAULT_PHRASES))

    # «Человеческий» режим для источников без API: прокси, рабочие часы, браузерные профили
    proxy_url: str | None = None  # http://user:pass@host:port — лучше резидентный/мобильный
    work_hours: str = "8-23"
    browser_profiles_dir: str = "data/profiles"
    browser_headless: bool = True

    # Threads без API (поиск без логина). Если задан THREADS_ACCESS_TOKEN — используется официальный API
    threads_web_enabled: bool = True

    # Биржи без логина
    kwork_enabled: bool = True
    kwork_categories: StrList = Field(default_factory=lambda: ["11"])  # 11 = Разработка и IT; "all" = все категории (много мусора)
    kwork_pages: int = 3
    kwork_interval: int = 600
    weblancer_enabled: bool = True
    weblancer_urls: StrList = Field(default_factory=lambda: ["https://www.weblancer.net/jobs/"])
    weblancer_interval: int = 900
    freelance_ru_enabled: bool = True
    freelance_ru_queries: StrList = Field(default_factory=lambda: ["сайт", "лендинг", "бот", "автоматизация", "нейросеть", "мультик"])
    freelance_ru_interval: int = 900

    # Яндекс.Карты без API + аудит сайтов организаций (мёртвый / без HTTPS / не мобильный → лид на редизайн)
    yandex_web_enabled: bool = True
    site_audit_enabled: bool = True
    site_audit_per_poll: int = 30

    # Instagram через залогиненный браузерный профиль (python -m leadhunter.browser.login instagram)
    ig_web_enabled: bool = False
    ig_web_per_poll: int = 3

    # Отправка черновика клиенту через ваш Telegram-аккаунт: лимит в день (0 = выключено)
    tg_outreach_limit: int = 0

    # Вебхук для CRM / n8n / Google Sheets: POST JSON с каждым лидом
    webhook_url: str | None = None

    # Рабочая группа и команда
    hot_score: int = 8  # с этого балла лид идёт в «🔥 Горячие» со звуком
    deliver_dm: bool = False  # дублировать лидов владельцам в личку, даже если настроена группа
    sla_minutes: int = 10  # лид не взят столько минут → пинг в «Горячие»
    sla_min_score: int = 7  # SLA считаем только для лидов с таким баллом и выше
    followup_hours: int = 24  # напомнить, если после «Написал» нет ответа
    spam_chats_per_hour: int = 3  # автор засветился в стольких чатах за час → рассылка, игнорируем
    discover_join_per_day: int = 5  # автопоиск чатов: сколько вступать в день

    # Mini App (кабинет внутри Telegram): публичный HTTPS-адрес, где доступен этот сервер
    miniapp_url: str | None = None
    miniapp_port: int = 8080
    miniapp_tunnel: str = ""  # "cloudflare" — поднять бесплатный quick tunnel и не думать о домене

    # Telegram-аккаунт (Telethon) для чтения чатов. Бот сам в чужие чаты не попадёт.
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_session: str = "data/userbot"
    tg_chats: StrList = Field(default_factory=list)  # пусто = все группы аккаунта
    tg_join_chats: StrList = Field(default_factory=list)  # @username или ссылки-приглашения, вступить при старте
    tg_search_interval: int = 600
    tg_search_words: StrList = Field(
        default_factory=lambda: ["сайт", "лендинг", "бот", "автоматизация", "нейросеть", "мультик", "анимация"]
    )

    # Threads: официальный Keyword Search API (разрешение threads_keyword_search)
    threads_access_token: str | None = None
    threads_interval: int = 900

    # Instagram Graph API: поиск по хэштегам (бизнес-аккаунт + Facebook-приложение)
    ig_access_token: str | None = None
    ig_user_id: str | None = None
    ig_hashtags: StrList = Field(
        default_factory=lambda: ["нуженсайт", "ищуразработчика", "нуженбот", "нужнаавтоматизация", "заказатьсайт", "ищуподрядчика"]
    )
    ig_interval: int = 1800

    # Яндекс.Карты: бизнесы без сайта (Geosearch API, ключ бесплатный)
    yandex_geosearch_key: str | None = None
    # «Россия» — города из leadhunter/cities.py; за один проход берётся yandex_per_poll пар «рубрика + город»
    yandex_cities: StrList = Field(default_factory=lambda: ["Россия"])
    yandex_queries: StrList = Field(default_factory=lambda: list(SITE_RUBRICS))
    yandex_per_poll: int = 6
    yandex_interval: int = 6 * 3600

    # Биржи с публичным RSS заказов
    rss_feeds: StrList = Field(default_factory=lambda: ["https://www.fl.ru/rss/all.xml"])
    rss_interval: int = 300

    # hh.ru — проектные вакансии (API публичный, нужен User-Agent с контактом).
    # Выключен по умолчанию: без своего User-Agent с настоящей почтой hh отвечает 400 на каждый запрос
    hh_enabled: bool = False
    hh_user_agent: str = ""  # например: LeadHunter/0.1 (вы@ваш-домен.ru); example.com у hh в чёрном списке
    hh_queries: StrList = Field(
        default_factory=lambda: ["разработка сайта", "telegram бот", "автоматизация нейросеть", "ИИ анимация"]
    )
    hh_interval: int = 900

    # VK: newsfeed.search по постам (нужен user-токен)
    vk_token: str | None = None
    vk_interval: int = 300

    # Поиск по Threads/Instagram/форумам через поисковики (Serper — платный ключ, иначе DuckDuckGo)
    serper_api_key: str | None = None
    web_search_sites: StrList = Field(default_factory=lambda: ["threads.net", "instagram.com", "vc.ru", "pikabu.ru"])
    web_search_interval: int = 1800

    # ИИ-фильтр (любой OpenAI-совместимый API). Без ключа работает словарный скоринг.
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    llm_min_score: int = 2  # словарный балл, с которого текст отправляется ИИ (2 = хотя бы одно слово по теме)
    llm_max_per_hour: int = 300

    # Ежедневный дайджест
    digest_hour: int | None = 9  # пусто = дайджест выключен
    digest_tz: str = "Europe/Moscow"

    # === Контент-фабрика ===
    content_dir: str = "data/content"
    content_hour: int = 10  # автопилот приносит черновик в этот час
    # Gemini: один ключ (aistudio.google.com, есть бесплатный лимит) — тексты, картинки, озвучка и ИИ-видео (Veo).
    # Если OPENAI_API_KEY не задан, тексты тоже идут в Gemini через OpenAI-совместимый endpoint.
    gemini_api_key: str | None = None
    gemini_model: str = ""  # текстовая модель Gemini; пусто = значение по умолчанию из content/gemini.py
    gemini_image_model: str = ""
    gemini_video_model: str = ""
    gemini_tts_model: str = ""
    gemini_tts_voice: str = "Kore"
    # OpenAI-медиа (если нет Gemini): картинки и озвучка
    image_model: str = ""  # gpt-image-1 / dall-e-3; пусто = обложки по шаблону (без ИИ)
    tts_model: str = ""  # tts-1 / gpt-4o-mini-tts; пусто = видео без озвучки
    tts_voice: str = "alloy"
    threads_user_id: str | None = None  # публикация в Threads (вместе с THREADS_ACCESS_TOKEN)
    yt_client_id: str | None = None  # YouTube Shorts: OAuth-приложение + refresh token
    yt_client_secret: str | None = None
    yt_refresh_token: str | None = None

    def __init__(self, **values):
        try:
            super().__init__(**values)
        except ValidationError as e:
            raise ConfigError(_explain(e)) from None

    @field_validator("*", mode="before")
    @classmethod
    def _empty_means_unset(cls, v, info):
        """«KEY=» в .env — это «не задано»: берём значение по умолчанию, а не падаем на разборе числа.
        Пустой список остаётся пустым (RSS_FEEDS= выключает источник), пустое необязательное — None."""
        if not isinstance(v, str):
            return v
        field = cls.model_fields[info.field_name]
        if get_origin(field.annotation) is list:
            return v
        if not v.strip():
            if type(None) in get_args(field.annotation):
                return None
            return v if field.is_required() else field.get_default(call_default_factory=True)
        if field.annotation is bool and v.strip().lower() in YES | NO:
            return v.strip().lower() in YES
        return v.strip()

    @field_validator("owner_ids", mode="before")
    @classmethod
    def _split_ints(cls, v):
        if isinstance(v, str):
            try:
                return [int(x) for x in v.split(",") if x.strip()]
            except ValueError:
                raise ValueError("нужны числа через запятую — ваш Telegram id, например 123456789 "
                                 "(узнать: напишите @userinfobot)") from None
        return v

    @field_validator(
        "tg_chats", "tg_join_chats", "tg_search_words", "ig_hashtags", "yandex_cities", "yandex_queries",
        "rss_feeds", "hh_queries", "web_search_sites", "search_phrases", "kwork_categories", "weblancer_urls",
        "freelance_ru_queries", mode="before",
    )
    @classmethod
    def _split_strs(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("bot_token")
    @classmethod
    def _check_token(cls, v):
        if not v.strip():
            raise ValueError("не задан — вставьте токен, который выдал @BotFather")
        if v == PLACEHOLDER_TOKEN:
            raise ValueError("это пример из .env.example — вставьте свой токен от @BotFather")
        if not re.fullmatch(r"\d+:\S+", v):
            raise ValueError("не похоже на токен от @BotFather (нужна строка вида 123456:ABC…, без пробелов)")
        return v

    @field_validator("digest_hour", "content_hour")
    @classmethod
    def _check_hour(cls, v):
        if v is not None and not 0 <= v <= 23:
            raise ValueError("нужен час от 0 до 23")
        return v

    @field_validator("digest_tz")
    @classmethod
    def _check_tz(cls, v):
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError("неизвестный часовой пояс. Примеры: Europe/Moscow, Asia/Yekaterinburg, "
                             "Asia/Novosibirsk") from None
        return v

    @field_validator("work_hours")
    @classmethod
    def _check_work_hours(cls, v):
        m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", v)
        if not m or int(m.group(1)) > 24 or int(m.group(2)) > 24:
            raise ValueError("нужны часы работы в виде 8-23 (круглые сутки: 0-24)")
        return f"{int(m.group(1))}-{int(m.group(2))}"

    @field_validator(
        "kwork_interval", "weblancer_interval", "freelance_ru_interval", "tg_search_interval", "threads_interval",
        "ig_interval", "yandex_interval", "rss_interval", "hh_interval", "vk_interval", "web_search_interval",
    )
    @classmethod
    def _check_interval(cls, v):
        if v <= 0:
            raise ValueError("нужно число секунд больше нуля")
        return v

    @field_validator("yandex_per_poll")
    @classmethod
    def _check_maps_per_poll(cls, v):
        if not 1 <= v <= 30:
            raise ValueError("нужно число от 1 до 30: столько запросов к картам бот делает за один проход")
        return v

    @field_validator("miniapp_port")
    @classmethod
    def _check_port(cls, v):
        if not 1 <= v <= 65535:
            raise ValueError("нужен номер порта от 1 до 65535 (обычно 8080)")
        return v

    @field_validator("miniapp_tunnel")
    @classmethod
    def _check_tunnel(cls, v):
        v = v.lower()
        if v not in ("", "cloudflare"):
            raise ValueError("допустимо только cloudflare или пусто")
        return v

    @model_validator(mode="after")
    def _hh_needs_contact(self):
        ua = self.hh_user_agent.lower()
        if self.hh_enabled and ("@" not in ua or "example.com" in ua):
            log.warning("hh.ru выключен: в HH_USER_AGENT нужна ваша настоящая почта, например "
                        "«LeadHunter/0.1 (вы@ваш-домен.ru)». Без неё hh отвечает ошибкой 400.")
            self.hh_enabled = False
        return self


def main() -> None:
    """python -m leadhunter.config — проверить .env, не запуская бота (вызывается из install.sh и update.sh)."""
    try:
        s = Settings()
    except ConfigError as e:
        sys.exit(f"❌ {e}")
    print(f"✅ Настройки в порядке. База: {s.db_path}, порог: {s.score_threshold}, "
          f"дайджест: {'выключен' if s.digest_hour is None else f'{s.digest_hour}:00 {s.digest_tz}'}")


if __name__ == "__main__":
    main()
