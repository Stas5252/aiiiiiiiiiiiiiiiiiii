from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Списки в .env задаются через запятую: OWNER_IDS=123,456
StrList = Annotated[list[str], NoDecode]
IntList = Annotated[list[int], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    owner_ids: IntList = Field(default_factory=list)
    db_path: str = "data/leads.db"
    score_threshold: int = 5

    # Telegram-аккаунт (Telethon) для чтения чатов. Бот сам в чужие чаты не попадёт.
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_session: str = "data/userbot"
    tg_chats: StrList = Field(default_factory=list)  # пусто = все группы аккаунта
    tg_search_interval: int = 600
    tg_search_words: StrList = Field(
        default_factory=lambda: ["сайт", "лендинг", "бот", "автоматизация", "нейросеть", "мультик", "анимация"]
    )

    # Яндекс.Карты: бизнесы без сайта (Geosearch API, ключ бесплатный)
    yandex_geosearch_key: str | None = None
    yandex_cities: StrList = Field(default_factory=lambda: ["Москва"])
    yandex_queries: StrList = Field(
        default_factory=lambda: ["салон красоты", "стоматология", "автосервис", "кофейня", "фитнес клуб", "ремонт квартир"]
    )
    yandex_interval: int = 6 * 3600

    # Биржи с публичным RSS заказов
    rss_feeds: StrList = Field(default_factory=lambda: ["https://www.fl.ru/rss/all.xml"])
    rss_interval: int = 300

    # hh.ru — проектные вакансии (API публичный, нужен User-Agent с контактом)
    hh_enabled: bool = True
    hh_user_agent: str = "LeadHunter/0.1 (contact@example.com)"
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
    search_phrases: StrList = Field(
        default_factory=lambda: [
            "нужен сайт",
            "ищу кто сделает сайт",
            "нужен telegram бот",
            "ищу разработчика бота",
            "нужна автоматизация",
            "ищу кто сделает мультик",
            "нужен ролик нейросеть",
            "посоветуйте кто делает сайты",
        ]
    )

    # Необязательная ИИ-проверка лидов (любой OpenAI-совместимый API)
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    @field_validator("owner_ids", mode="before")
    @classmethod
    def _split_ints(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.split(",") if x.strip()]
        return v

    @field_validator(
        "tg_chats", "tg_search_words", "yandex_cities", "yandex_queries", "rss_feeds",
        "hh_queries", "web_search_sites", "search_phrases", mode="before",
    )
    @classmethod
    def _split_strs(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v
