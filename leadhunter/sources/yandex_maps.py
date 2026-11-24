import httpx

from ..cities import MapsPlan
from ..config import Settings
from ..hosts import company_site, shared_hosts
from ..models import Lead
from .base import Source

API = "https://search-maps.yandex.ru/v1/"
PAGE = 50
MAX_PAGES = 2  # лимит бесплатного ключа — 500 запросов/сутки


def parse_features(data: dict, query: str, city: str) -> list[Lead]:
    """Организации без сайта — готовые лиды на сайт/бота."""
    leads = []
    metas = [(f.get("properties") or {}).get("CompanyMetaData") or {} for f in data.get("features", [])]
    # справочник не из списка узнаём так же, как в источнике без ключа: один домен у компаний с разными названиями
    shared = shared_hosts([{"title": m.get("name"), "urls": [m["url"]]} for m in metas if m.get("url")])
    for meta in metas:
        site, where = company_site({"urls": [meta["url"]]} if meta.get("url") else {}, shared)
        if not meta.get("id") or site:
            continue
        no_site = f"Своего сайта в карточке нет: вместо него указана страница на {where}." if where else "Сайта в карточке нет."
        phones = ", ".join(p.get("formatted", "") for p in meta.get("Phones", []) if p.get("formatted"))
        categories = ", ".join(c.get("name", "") for c in meta.get("Categories", []))
        first = next((c["name"] for c in meta.get("Categories", []) if c.get("name")), query)
        leads.append(Lead(
            source="yandex_maps",
            external_id=str(meta["id"]),
            title=meta.get("name", ""),
            text=f"{categories or query}, {city}. {meta.get('address', '')}. {no_site}",
            url=f"https://yandex.ru/maps/org/{meta['id']}",
            contact=phones,
            preset_score=6,
            extra={"meta": {k: v for k, v in (("category", first), ("city", city), ("address", meta.get("address", ""))) if v}},
        ))
    return leads


class YandexMapsSource(Source):
    name = "yandex_maps"

    def __init__(self, s: Settings, db=None):
        self.key = s.yandex_geosearch_key
        self.plan = MapsPlan(s.yandex_cities, s.yandex_queries, s.yandex_per_poll, db)
        self.interval = s.yandex_interval

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        pairs, cursor = await self.plan.batch()
        async with httpx.AsyncClient(timeout=30) as c:
            for city, q in pairs:
                for page in range(MAX_PAGES):
                    params = {"apikey": self.key, "text": f"{q} {city}", "lang": "ru_RU", "type": "biz",
                              "results": PAGE, "skip": page * PAGE}
                    r = await c.get(API, params=params)
                    r.raise_for_status()
                    data = r.json()
                    leads += parse_features(data, q, city)
                    if len(data.get("features", [])) < PAGE:
                        break
        await self.plan.commit(cursor)
        return leads
