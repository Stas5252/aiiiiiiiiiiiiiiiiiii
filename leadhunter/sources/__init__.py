from ..config import Settings
from ..db import Database
from ..phrases import PhraseProvider
from .base import Source
from .freelance_ru import FreelanceRuSource
from .hh import HHSource
from .instagram import InstagramSource
from .instagram_web import InstagramWebSource
from .kwork import KworkSource
from .rss_boards import RSSBoardsSource
from .telegram_chats import TelegramChatsSource
from .threads import ThreadsSource
from .threads_web import ThreadsWebSource
from .vk import VKSource
from .web_search import WebSearchSource
from .weblancer import WeblancerSource
from .yandex_maps import YandexMapsSource
from .yandex_maps_web import YandexMapsWebSource


def build_sources(s: Settings, db: Database, phrases: PhraseProvider) -> list[Source]:
    """Собирает источники: официальный API, если есть ключ, иначе — путь без API («как человек»)."""
    sources: list[Source] = []
    covered: set[str] = set()  # домены, по которым уже есть прямой источник — не дублируем через поисковик

    if s.tg_api_id and s.tg_api_hash:
        sources.append(TelegramChatsSource(s))

    if s.threads_access_token:
        sources.append(ThreadsSource(s, phrases))
        covered.add("threads.net")
    elif s.threads_web_enabled:
        sources.append(ThreadsWebSource(phrases, s.threads_interval, s.proxy_url))
        covered.add("threads.net")

    if s.ig_access_token and s.ig_user_id:
        sources.append(InstagramSource(s, db))
        covered.add("instagram.com")
    elif s.ig_web_enabled:
        sources.append(InstagramWebSource(s.ig_hashtags, s.ig_interval, s.browser_profiles_dir, s.proxy_url,
                                          s.browser_headless, s.ig_web_per_poll))
        covered.add("instagram.com")

    if s.kwork_enabled:
        cats = ["" if c.lower() in ("all", "*") else c for c in s.kwork_categories]
        sources.append(KworkSource(cats, s.kwork_pages, s.kwork_interval, s.proxy_url))
    if s.weblancer_enabled:
        sources.append(WeblancerSource(s.weblancer_urls, s.weblancer_interval, s.proxy_url))
    if s.freelance_ru_enabled:
        sources.append(FreelanceRuSource(s.freelance_ru_queries, s.freelance_ru_interval, s.proxy_url))
    if s.rss_feeds:
        sources.append(RSSBoardsSource(s))
    if s.hh_enabled:
        sources.append(HHSource(s))

    if s.yandex_geosearch_key:
        sources.append(YandexMapsSource(s))
    elif s.yandex_web_enabled:
        sources.append(YandexMapsWebSource(s.yandex_cities, s.yandex_queries, s.yandex_interval, s.proxy_url,
                                           s.site_audit_enabled, s.site_audit_per_poll))

    if s.vk_token:
        sources.append(VKSource(s, phrases))

    sites = [x for x in s.web_search_sites if x not in covered]
    if sites:
        sources.append(WebSearchSource(s, phrases, sites))

    for src in sources:
        src.work_hours = s.work_hours
        src.tz = s.digest_tz
    return sources
