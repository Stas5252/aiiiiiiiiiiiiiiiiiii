import time
from datetime import datetime, timezone

import httpx

from ..config import Settings
from ..models import Lead
from .base import Source

API = "https://graph.threads.net/v1.0/keyword_search"
FIELDS = "id,text,permalink,timestamp,username,is_reply"


def parse_threads(data: dict) -> list[Lead]:
    leads = []
    for p in data.get("data", []):
        if not p.get("text") or p.get("is_reply"):
            continue
        ts = p.get("timestamp")
        found = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z") if ts else datetime.now(timezone.utc)
        username = p.get("username", "")
        leads.append(Lead(
            source="threads",
            external_id=str(p["id"]),
            text=p["text"],
            url=p.get("permalink") or (f"https://www.threads.net/@{username}" if username else ""),
            author=f"@{username}" if username else "",
            contact=f"https://www.threads.net/@{username}" if username else "",
            found_at=found,
        ))
    return leads


class ThreadsSource(Source):
    """Официальный Threads Keyword Search API: свежие посты по фразам-намерениям."""

    name = "threads"

    def __init__(self, s: Settings):
        self.token = s.threads_access_token
        self.phrases = s.search_phrases
        self.interval = s.threads_interval
        self.since = int(time.time()) - 24 * 3600

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        now = int(time.time())
        async with httpx.AsyncClient(timeout=30) as c:
            for q in self.phrases:
                r = await c.get(API, params={"q": q, "search_type": "RECENT", "fields": FIELDS,
                                             "since": self.since, "access_token": self.token})
                r.raise_for_status()
                leads += parse_threads(r.json())
        self.since = now - 300
        return leads
