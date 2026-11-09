import asyncio
import logging

import httpx

from ..config import Settings
from ..models import Lead
from ..phrases import PhraseProvider
from .base import Source

log = logging.getLogger(__name__)
SERPER = "https://google.serper.dev/search"


class WebSearchSource(Source):
    """Threads, Instagram, vc.ru и т.п. не дают бесплатного поиска по постам —
    ищем свежие публичные посты через поисковики по site:домен."""

    name = "web"

    def __init__(self, s: Settings, phrases: PhraseProvider, sites: list[str] | None = None):
        self.sites = sites if sites is not None else s.web_search_sites
        self.phrases = phrases
        self.interval = s.web_search_interval
        self.serper_key = s.serper_api_key

    async def _serper(self, query: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=30, headers={"X-API-KEY": self.serper_key}) as c:
            r = await c.post(SERPER, json={"q": query, "gl": "ru", "hl": "ru", "tbs": "qdr:d", "num": 20})
            r.raise_for_status()
            return [{"title": o.get("title", ""), "url": o.get("link", ""), "text": o.get("snippet", "")}
                    for o in r.json().get("organic", [])]

    async def _ddg(self, query: str) -> list[dict]:
        from ddgs import DDGS

        def search():
            try:
                return DDGS().text(query, region="ru-ru", timelimit="w", max_results=20)
            except Exception as e:  # ddgs кидает исключение на пустую выдачу
                log.debug("ddgs %r: %s", query, e)
                return []

        rows = await asyncio.to_thread(search)
        return [{"title": r.get("title", ""), "url": r.get("href", ""), "text": r.get("body", "")} for r in rows]

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        phrases = await self.phrases.get()
        for site in self.sites:
            for phrase in phrases:
                query = f'site:{site} "{phrase}"'
                rows = await (self._serper(query) if self.serper_key else self._ddg(query))
                for r in rows:
                    if not r["url"] or site not in r["url"]:
                        continue
                    leads.append(Lead(source=f"web:{site}", external_id=r["url"], title=r["title"],
                                      text=r["text"], url=r["url"]))
                if not self.serper_key:
                    await asyncio.sleep(3)  # DDG без ключа — не долбим
        return leads
