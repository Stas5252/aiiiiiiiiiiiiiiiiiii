import json
import re
from datetime import datetime, timezone

from ..http import Fetcher
from ..models import Lead
from .base import Source, strip_html

BASE = "https://www.weblancer.net"
CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def parse_weblancer_html(body: str) -> list[Lead]:
    """Weblancer на Next.js: список заказов лежит в RSC-payload, разбитом на self.__next_f.push(...)."""
    payload = "".join(json.loads('"' + c + '"') for c in CHUNK_RE.findall(body))
    k = payload.find('"jobs":[')
    if k < 0:
        return []
    try:
        jobs, _ = json.JSONDecoder().raw_decode(payload[k + len('"jobs":'):])
    except json.JSONDecodeError:
        return []
    leads = []
    for j in jobs:
        if not j.get("id") or j.get("status") not in (None, "opened"):
            continue
        text = strip_html(j.get("description") or j.get("snippet") or "")
        if j.get("budget") and str(j["budget"]) not in ("0", "None"):
            text += f"\nБюджет: {j['budget']} $"
        ts = j.get("bidding_time")
        found = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts and str(ts).isdigit() else datetime.now(timezone.utc)
        user = j.get("user") if isinstance(j.get("user"), dict) else {}
        leads.append(Lead(
            source="weblancer",
            external_id=str(j["id"]),
            title=(j.get("name") or "").strip(),
            text=text,
            url=BASE + j["url"] if j.get("url") else "",
            author=" ".join(filter(None, [user.get("first_name"), user.get("last_name")])),
            found_at=found,
            intent_implied=True,
        ))
    return leads


class WeblancerSource(Source):
    name = "weblancer"
    human = True

    def __init__(self, urls: list[str], interval: int, proxy: str | None):
        self.urls = urls
        self.interval = interval
        self.fetcher = Fetcher(proxy, pause=(4, 10))

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        for url in self.urls:
            r = await self.fetcher.get(url)
            r.raise_for_status()
            leads += parse_weblancer_html(r.text)
        return leads
