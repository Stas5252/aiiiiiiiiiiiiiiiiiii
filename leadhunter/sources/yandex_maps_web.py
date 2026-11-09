import asyncio
import json
import logging
import re

from ..audit import audit_site
from ..http import Fetcher
from ..models import Lead
from .base import Source

log = logging.getLogger(__name__)
URL = "https://yandex.ru/maps/"
ANCHOR = re.compile(r'"items":\s*(?=\[\s*\{\s*"type":\s*"business")')


def parse_yandex_items(body: str) -> list[dict]:
    """Страница поиска Яндекс.Карт содержит JSON выдачи (25 организаций) с сайтами, телефонами и соцсетями."""
    m = ANCHOR.search(body)
    if not m:
        return []
    try:
        items, _ = json.JSONDecoder().raw_decode(body[m.end():])
    except json.JSONDecodeError:
        return []
    return [x for x in items if x.get("type") == "business" and x.get("id") and x.get("address")]


def business_lead(item: dict, query: str, city: str) -> Lead:
    phones = ", ".join(p.get("number", "") for p in item.get("phones") or [] if p.get("number"))
    socials = ", ".join(s.get("href", "") for s in item.get("socialLinks") or [] if s.get("href"))
    cats = ", ".join(c.get("name", "") for c in item.get("categories") or [])
    rating = item.get("ratingData") or {}
    extra = f" Рейтинг {rating['ratingValue']:.1f}, отзывов {rating.get('reviewCount', 0)}." if rating.get("ratingValue") else ""
    return Lead(
        source="yandex_maps",
        external_id=str(item["id"]),
        title=item.get("title", ""),
        text=f"{cats or query}, {city}. {item.get('address', '')}.{extra} Сайта в карточке нет.",
        url=f"https://yandex.ru/maps/org/{item['id']}",
        contact=", ".join(filter(None, [phones, socials])),
        preset_score=6,
    )


def audit_lead(item: dict, site: str, problem: str, city: str) -> Lead:
    phones = ", ".join(p.get("number", "") for p in item.get("phones") or [] if p.get("number"))
    cats = ", ".join(c.get("name", "") for c in item.get("categories") or [])
    return Lead(
        source="site_audit",
        external_id=f"audit:{item['id']}",
        title=item.get("title", ""),
        text=f"{cats}, {city}. Сайт {site}: {problem}. Повод предложить новый сайт.",
        url=f"https://yandex.ru/maps/org/{item['id']}",
        contact=phones,
        preset_score=5,
    )


def clean_site(url: str) -> str:
    return url.split("?")[0].rstrip("/")


class YandexMapsWebSource(Source):
    """Яндекс.Карты без API: поиск «запрос город», организации без сайта + аудит существующих сайтов."""

    name = "yandex_maps"
    human = True

    def __init__(self, cities: list[str], queries: list[str], interval: int, proxy: str | None,
                 audit: bool = True, audit_per_poll: int = 30):
        self.cities = cities
        self.queries = queries
        self.interval = interval
        self.proxy = proxy
        self.audit = audit
        self.audit_per_poll = audit_per_poll
        self.fetcher = Fetcher(proxy, pause=(12, 30))

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        to_audit: list[tuple[dict, str, str]] = []
        for city in self.cities:
            for q in self.queries:
                r = await self.fetcher.get(URL, params={"text": f"{q} {city}"})
                r.raise_for_status()
                for item in parse_yandex_items(r.text):
                    if item.get("urls"):
                        to_audit.append((item, clean_site(item["urls"][0]), city))
                    else:
                        leads.append(business_lead(item, q, city))
        if self.audit and to_audit:
            sem = asyncio.Semaphore(5)

            async def check(item, site, city):
                async with sem:
                    problem = await audit_site(site, self.proxy)
                return audit_lead(item, site, problem, city) if problem else None

            results = await asyncio.gather(*(check(*t) for t in to_audit[: self.audit_per_poll]), return_exceptions=True)
            leads += [x for x in results if isinstance(x, Lead)]
        return leads
