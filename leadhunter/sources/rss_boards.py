import asyncio
from datetime import datetime, timezone
from time import mktime
from urllib.parse import urlparse

import feedparser

from ..config import Settings
from ..http import Fetcher
from ..models import Lead
from .base import Source, strip_html



def parse_feed(raw: bytes, feed_url: str) -> list[Lead]:
    parsed = feedparser.parse(raw)
    host = urlparse(feed_url).netloc.removeprefix("www.")
    leads = []
    for e in parsed.entries:
        link = e.get("link", "")
        ext_id = e.get("id") or link
        if not ext_id:
            continue
        found_at = datetime.now(timezone.utc)
        if e.get("published_parsed"):
            found_at = datetime.fromtimestamp(mktime(e.published_parsed), tz=timezone.utc)
        leads.append(Lead(
            source=f"rss:{host}",
            external_id=ext_id,
            title=strip_html(e.get("title", "")),
            text=strip_html(e.get("summary", "") or e.get("description", "")),
            url=link,
            author=e.get("author", ""),
            found_at=found_at,
            intent_implied=True,  # на бирже каждая запись — заказ
        ))
    return leads


class RSSBoardsSource(Source):
    name = "rss"
    human = True

    def __init__(self, s: Settings):
        self.feeds = s.rss_feeds
        self.interval = s.rss_interval
        self.fetcher = Fetcher(s.proxy_url, pause=(2, 5))

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        for url in self.feeds:
            r = await self.fetcher.get(url)  # FL.ru режет обычные HTTP-клиенты, нужен TLS-отпечаток браузера
            r.raise_for_status()
            leads += await asyncio.to_thread(parse_feed, r.content, url)
        return leads
