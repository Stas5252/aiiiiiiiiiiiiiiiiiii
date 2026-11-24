"""Место сайта в поиске по запросу «рубрика + город». Google через Serper, если есть ключ; без ключа — бесплатный
поиск ddgs (он сам выбирает поисковик и нередко возвращает мало результатов или ничего)."""
import asyncio

import httpx

from .fetch import puny
from .site import domain_of

SERPER = "https://google.serper.dev/search"
DEPTH = 20
FREE = "бесплатный поиск"
RETRY_PAUSE = 3


async def search_urls(query: str, serper_key: str | None = None) -> tuple[str, list[str]]:
    """(название поисковика, адреса из выдачи по порядку). Бросает исключение, если поисковик не ответил."""
    if serper_key:
        async with httpx.AsyncClient(timeout=30, headers={"X-API-KEY": serper_key}) as c:
            r = await c.post(SERPER, json={"q": query, "gl": "ru", "hl": "ru", "num": DEPTH})
            r.raise_for_status()
            return "Google", [o.get("link", "") for o in r.json().get("organic", [])]
    from ddgs import DDGS

    def run():
        try:
            return DDGS().text(query, region="ru-ru", max_results=DEPTH)
        except Exception as e:
            if "no results" in str(e).lower():
                return []
            raise

    rows = await asyncio.to_thread(run)
    if not rows:  # пустой ответ у ddgs часто случайный — одна повторная попытка
        await asyncio.sleep(RETRY_PAUSE)
        rows = await asyncio.to_thread(run)
    return FREE, [r.get("href", "") for r in rows]


def position(domain: str, urls: list[str]) -> int | None:
    """Место сайта в выдаче, с единицы. None — в первых результатах его нет."""
    domain = puny(domain)
    for i, url in enumerate(urls, 1):
        host = puny(domain_of(url)) if url else ""
        if host and (host == domain or host.endswith("." + domain)):
            return i
    return None
