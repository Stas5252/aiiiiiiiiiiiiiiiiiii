import json
import re
from datetime import datetime, timezone

from ..http import Fetcher, human_pause
from ..models import Lead
from ..phrases import PhraseProvider
from .base import Source

SEARCH = "https://www.threads.net/search"
SCRIPT_RE = re.compile(r'<script type="application/json"[^>]*data-sjs[^>]*>(.*?)</script>', re.S)


def _walk(obj, key, out):
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj[key])
        for v in obj.values():
            _walk(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, key, out)


def parse_threads_html(body: str) -> list[Lead]:
    """Страница поиска Threads без логина содержит Relay-кэш с постами в <script data-sjs>."""
    leads = []
    for raw in SCRIPT_RE.findall(body):
        if "thread_items" not in raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        groups: list = []
        _walk(data, "thread_items", groups)
        for items in groups:
            for item in items:
                post = item.get("post") or {}
                text = ((post.get("caption") or {}).get("text") or "").strip()
                user = (post.get("user") or {}).get("username") or ""
                code = post.get("code")
                if not text or not post.get("pk"):
                    continue
                ts = post.get("taken_at")
                leads.append(Lead(
                    source="threads",
                    external_id=str(post["pk"]),
                    text=text,
                    url=f"https://www.threads.net/@{user}/post/{code}" if user and code else "",
                    author=f"@{user}" if user else "",
                    contact=f"https://www.threads.net/@{user}" if user else "",
                    found_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc),
                ))
    return leads


class ThreadsWebSource(Source):
    """Threads без API и без логина: поиск по фразам-намерениям, ~15-25 свежих постов на запрос."""

    name = "threads"
    human = True

    def __init__(self, phrases: PhraseProvider, interval: int, proxy: str | None):
        self.phrases = phrases
        self.interval = interval
        self.fetcher = Fetcher(proxy, pause=(6, 15))

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        for q in await self.phrases.get():
            r = await self.fetcher.get(SEARCH, params={"q": q, "serp_type": "recent"})
            r.raise_for_status()
            leads += parse_threads_html(r.text)
        return leads
