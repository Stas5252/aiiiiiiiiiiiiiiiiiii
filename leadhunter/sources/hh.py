from datetime import datetime

import httpx

from ..config import Settings
from ..models import Lead
from .base import Source, strip_html

API = "https://api.hh.ru/vacancies"


def parse_vacancies(data: dict) -> list[Lead]:
    leads = []
    for v in data.get("items", []):
        snippet = v.get("snippet") or {}
        text = " ".join(filter(None, [snippet.get("responsibility"), snippet.get("requirement")]))
        leads.append(Lead(
            source="hh.ru",
            external_id=str(v["id"]),
            title=v.get("name", ""),
            text=strip_html(text),
            url=v.get("alternate_url", ""),
            author=(v.get("employer") or {}).get("name", ""),
            found_at=datetime.fromisoformat(v["published_at"]) if v.get("published_at") else datetime.now().astimezone(),
            intent_implied=True,
        ))
    return leads


class HHSource(Source):
    name = "hh.ru"

    def __init__(self, s: Settings):
        self.queries = s.hh_queries
        self.interval = s.hh_interval
        self.ua = s.hh_user_agent

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": self.ua}) as c:
            for q in self.queries:
                r = await c.get(API, params={"text": q, "per_page": 50, "order_by": "publication_time", "period": 1})
                r.raise_for_status()
                leads += parse_vacancies(r.json())
        return leads
