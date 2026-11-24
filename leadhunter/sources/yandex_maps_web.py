import asyncio
import json
import logging
import re
from itertools import zip_longest

from ..audit import audit_site
from ..cities import MapsPlan
from ..hosts import clean_site, company_site, shared_hosts  # noqa: F401 — clean_site берут отсюда досье и тесты
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


def company_meta(item: dict, city: str, query: str = "", site: str = "") -> dict:
    """Что карты знают о компании — сохраняется с лидом и идёт в досье (конкуренты той же рубрики и города)."""
    cats = [c.get("name", "") for c in item.get("categories") or [] if c.get("name")]
    rating = item.get("ratingData") or {}
    meta = {"category": cats[0] if cats else query, "city": city, "address": item.get("address", ""), "site": site,
            "rating": rating.get("ratingValue"), "reviews": rating.get("reviewCount")}
    return {k: v for k, v in meta.items() if v}


def business_lead(item: dict, query: str, city: str, foreign: str = "") -> Lead:
    """Компания без своего сайта. foreign — чужая площадка (справочник, соцсеть), указанная в карточке вместо сайта."""
    phones = ", ".join(p.get("number", "") for p in item.get("phones") or [] if p.get("number"))
    socials = ", ".join(s.get("href", "") for s in item.get("socialLinks") or [] if s.get("href"))
    cats = ", ".join(c.get("name", "") for c in item.get("categories") or [])
    rating = item.get("ratingData") or {}
    extra = f" Рейтинг {rating['ratingValue']:.1f}, отзывов {rating.get('reviewCount', 0)}." if rating.get("ratingValue") else ""
    return Lead(
        source="yandex_maps",
        external_id=str(item["id"]),
        title=item.get("title", ""),
        text=f"{cats or query}, {city}. {item.get('address', '')}.{extra} "
             + (f"Своего сайта в карточке нет: вместо него указана страница на {foreign}." if foreign
                else "Сайта в карточке нет."),
        url=f"https://yandex.ru/maps/org/{item['id']}",
        contact=", ".join(filter(None, [phones, socials])),
        preset_score=6,
        extra={"meta": company_meta(item, city, query)},
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
        extra={"meta": company_meta(item, city, site=site)},
    )


class YandexMapsWebSource(Source):
    """Яндекс.Карты без API: поиск «запрос город», организации без сайта + аудит существующих сайтов."""

    name = "yandex_maps"
    human = True

    def __init__(self, cities: list[str], queries: list[str], interval: int, proxy: str | None, audit: bool = True,
                 audit_per_poll: int = 30, per_poll: int = 6, db=None):
        self.plan = MapsPlan(cities, queries, per_poll, db)
        self.interval = interval
        self.proxy = proxy
        self.audit = audit
        self.audit_per_poll = audit_per_poll
        self.fetcher = Fetcher(proxy, pause=(12, 30))

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        to_audit: list[list[tuple[dict, str, str]]] = []
        pairs, cursor = await self.plan.batch()
        for city, q in pairs:
            r = await self.fetcher.get(URL, params={"text": f"{q} {city}"})
            r.raise_for_status()
            items = parse_yandex_items(r.text)
            shared = shared_hosts(items)
            with_site = []
            for item in items:
                site, foreign = company_site(item, shared)
                if site:
                    with_site.append((item, site, city))
                else:
                    leads.append(business_lead(item, q, city, foreign))
            to_audit.append(with_site)
        # Проход отработал целиком — следующий начнётся с новых городов. После сбоя те же пары повторятся.
        await self.plan.commit(cursor)
        # Лимит проверок сайтов делится между запросами прохода поровну, а не достаётся первому городу
        queue = [t for group in zip_longest(*to_audit) for t in group if t][: self.audit_per_poll]
        if self.audit and queue:
            sem = asyncio.Semaphore(5)

            async def check(item, site, city):
                async with sem:
                    problem = await audit_site(site, self.proxy)
                return audit_lead(item, site, problem, city) if problem else None

            results = await asyncio.gather(*(check(*t) for t in queue), return_exceptions=True)
            leads += [x for x in results if isinstance(x, Lead)]
        return leads
