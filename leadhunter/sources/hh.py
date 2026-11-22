from datetime import datetime

import httpx

from ..config import Settings
from ..models import Lead
from .base import Source, SourceError, strip_html

API = "https://api.hh.ru/vacancies"
# Заглушка из настроек по умолчанию: hh.ru держит её в чёрном списке и отвечает 400 bad_user_agent
PLACEHOLDER_UA = "contact@example.com"


def explain_error(status: int, body: str) -> str:
    """Понятная причина отказа hh.ru вместо «400 Bad Request»."""
    if status == 400 and "bad_user_agent" in body:
        return "hh.ru отклонил User-Agent — задайте свой HH_USER_AGENT вида «Название/1.0 (ваша@почта)»"
    if status == 403:
        return "hh.ru закрыл доступ с этого IP (блокирует дата-центры) — нужен прокси или домашний IP"
    return f"hh.ru ответил {status}"


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

    def blocked_reason(self) -> str:
        if not self.ua.strip() or PLACEHOLDER_UA in self.ua:
            return "не задан свой HH_USER_AGENT — с заглушкой hh.ru отвечает отказом"
        return ""

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": self.ua}) as c:
            for q in self.queries:
                r = await c.get(API, params={"text": q, "per_page": 50, "order_by": "publication_time", "period": 1})
                if r.status_code >= 400:
                    raise SourceError(explain_error(r.status_code, r.text))
                leads += parse_vacancies(r.json())
        return leads
