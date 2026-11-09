from ..config import Settings
from ..db import Database
from .base import Source
from .hh import HHSource
from .instagram import InstagramSource
from .rss_boards import RSSBoardsSource
from .telegram_chats import TelegramChatsSource
from .threads import ThreadsSource
from .vk import VKSource
from .web_search import WebSearchSource
from .yandex_maps import YandexMapsSource


def build_sources(s: Settings, db: Database) -> list[Source]:
    """Собирает только те источники, для которых есть ключи/настройки."""
    sources: list[Source] = []
    if s.tg_api_id and s.tg_api_hash:
        sources.append(TelegramChatsSource(s))
    if s.threads_access_token:
        sources.append(ThreadsSource(s))
    if s.ig_access_token and s.ig_user_id:
        sources.append(InstagramSource(s, db))
    if s.rss_feeds:
        sources.append(RSSBoardsSource(s))
    if s.hh_enabled:
        sources.append(HHSource(s))
    if s.yandex_geosearch_key:
        sources.append(YandexMapsSource(s))
    if s.vk_token:
        sources.append(VKSource(s))
    # Через поисковик ищем только там, где нет официального доступа
    official = {"threads.net"} if s.threads_access_token else set()
    if s.ig_access_token:
        official.add("instagram.com")
    sites = [x for x in s.web_search_sites if x not in official]
    if sites:
        sources.append(WebSearchSource(s, sites))
    return sources
