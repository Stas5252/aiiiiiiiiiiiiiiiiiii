from ..config import Settings
from .base import Source
from .hh import HHSource
from .rss_boards import RSSBoardsSource
from .telegram_chats import TelegramChatsSource
from .vk import VKSource
from .web_search import WebSearchSource
from .yandex_maps import YandexMapsSource


def build_sources(s: Settings) -> list[Source]:
    """Собирает только те источники, для которых есть ключи/настройки."""
    sources: list[Source] = []
    if s.tg_api_id and s.tg_api_hash:
        sources.append(TelegramChatsSource(s))
    if s.rss_feeds:
        sources.append(RSSBoardsSource(s))
    if s.hh_enabled:
        sources.append(HHSource(s))
    if s.yandex_geosearch_key:
        sources.append(YandexMapsSource(s))
    if s.vk_token:
        sources.append(VKSource(s))
    if s.web_search_sites:
        sources.append(WebSearchSource(s))
    return sources
