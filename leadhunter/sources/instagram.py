import logging
from datetime import datetime, timezone

import httpx

from ..config import Settings
from ..db import Database
from ..models import Lead
from .base import Source

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v21.0"
FIELDS = "id,caption,permalink,timestamp"


def parse_media(data: dict, hashtag: str) -> list[Lead]:
    leads = []
    for m in data.get("data", []):
        if not m.get("caption"):
            continue
        ts = m.get("timestamp")
        found = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z") if ts else datetime.now(timezone.utc)
        leads.append(Lead(
            source="instagram",
            external_id=str(m["id"]),
            title=f"#{hashtag}",
            text=m["caption"],
            url=m.get("permalink", ""),
            found_at=found,
        ))
    return leads


class InstagramSource(Source):
    """Instagram Graph API: свежие посты по хэштегам. Лимит — 30 уникальных хэштегов в неделю,
    поэтому id хэштегов кэшируются в базе."""

    name = "instagram"

    def __init__(self, s: Settings, db: Database):
        self.token = s.ig_access_token
        self.user_id = s.ig_user_id
        self.hashtags = s.ig_hashtags
        self.interval = s.ig_interval
        self.db = db

    async def _hashtag_id(self, c: httpx.AsyncClient, tag: str) -> str | None:
        key = f"ig_hashtag:{tag}"
        cached = await self.db.get_setting(key)
        if cached:
            return cached
        r = await c.get(f"{GRAPH}/ig_hashtag_search", params={"user_id": self.user_id, "q": tag, "access_token": self.token})
        if r.status_code != 200:
            log.warning("instagram: не нашли хэштег #%s: %s", tag, r.text[:200])
            return None
        data = r.json().get("data") or []
        if not data:
            return None
        await self.db.set_setting(key, data[0]["id"])
        return data[0]["id"]

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        async with httpx.AsyncClient(timeout=30) as c:
            for tag in self.hashtags:
                tag_id = await self._hashtag_id(c, tag)
                if not tag_id:
                    continue
                r = await c.get(f"{GRAPH}/{tag_id}/recent_media",
                                params={"user_id": self.user_id, "fields": FIELDS, "limit": 50, "access_token": self.token})
                r.raise_for_status()
                leads += parse_media(r.json(), tag)
        return leads
