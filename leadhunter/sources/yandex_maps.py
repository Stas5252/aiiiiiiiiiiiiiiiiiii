import httpx

from ..config import Settings
from ..models import Lead
from .base import Source

API = "https://search-maps.yandex.ru/v1/"
PAGE = 50
MAX_PAGES = 2  # лимит бесплатного ключа — 500 запросов/сутки


def parse_features(data: dict, query: str, city: str) -> list[Lead]:
    """Организации без сайта — готовые лиды на сайт/бота."""
    leads = []
    for f in data.get("features", []):
        meta = (f.get("properties") or {}).get("CompanyMetaData") or {}
        if not meta.get("id") or meta.get("url"):
            continue
        phones = ", ".join(p.get("formatted", "") for p in meta.get("Phones", []) if p.get("formatted"))
        categories = ", ".join(c.get("name", "") for c in meta.get("Categories", []))
        leads.append(Lead(
            source="yandex_maps",
            external_id=str(meta["id"]),
            title=meta.get("name", ""),
            text=f"{categories or query}, {city}. {meta.get('address', '')}. Сайта в карточке нет.",
            url=f"https://yandex.ru/maps/org/{meta['id']}",
            contact=phones,
            preset_score=6,
        ))
    return leads


class YandexMapsSource(Source):
    name = "yandex_maps"

    def __init__(self, s: Settings):
        self.key = s.yandex_geosearch_key
        self.cities = s.yandex_cities
        self.queries = s.yandex_queries
        self.interval = s.yandex_interval

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        async with httpx.AsyncClient(timeout=30) as c:
            for city in self.cities:
                for q in self.queries:
                    for page in range(MAX_PAGES):
                        params = {"apikey": self.key, "text": f"{q} {city}", "lang": "ru_RU", "type": "biz",
                                  "results": PAGE, "skip": page * PAGE}
                        r = await c.get(API, params=params)
                        r.raise_for_status()
                        data = r.json()
                        leads += parse_features(data, q, city)
                        if len(data.get("features", [])) < PAGE:
                            break
        return leads
