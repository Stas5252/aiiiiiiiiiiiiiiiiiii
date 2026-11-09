import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..http import Fetcher
from ..models import Lead
from .base import Source, strip_html

URL = "https://freelance.ru/task"
MSK = ZoneInfo("Europe/Moscow")
CARD_RE = re.compile(r'<article class="task-card">(.*?)</article>', re.S)
TITLE_RE = re.compile(r'<a class="task-card__title-link" href="(/task/view/(\d+))"[^>]*>\s*(.*?)\s*</a>', re.S)
DESC_RE = re.compile(r'<p class="task-card__desc">(.*?)</p>', re.S)
CAT_RE = re.compile(r'task-chip--cat">(.*?)</span>', re.S)
DATE_RE = re.compile(r'title="(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})"')
BUDGET_RE = re.compile(r'task-card__budget[^"]*">\s*(.*?)\s*</div>', re.S)


def parse_freelance_ru_html(body: str) -> list[Lead]:
    leads = []
    for card in CARD_RE.findall(body):
        m = TITLE_RE.search(card)
        if not m:
            continue
        path, task_id, title = m.groups()
        desc = DESC_RE.search(card)
        cat = CAT_RE.search(card)
        text = strip_html(html.unescape(desc.group(1))) if desc else ""
        if cat:
            text += f"\nКатегория: {strip_html(cat.group(1))}"
        budget = BUDGET_RE.search(card)
        if budget:
            b = strip_html(html.unescape(budget.group(1)))
            if b and "договор" not in b.lower():
                text += f"\nГонорар: {b}"
        found = datetime.now(timezone.utc)
        d = DATE_RE.search(card)
        if d:
            found = datetime.strptime(d.group(1), "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        leads.append(Lead(
            source="freelance.ru",
            external_id=task_id,
            title=strip_html(html.unescape(title)),
            text=text,
            url=f"https://freelance.ru{path}",
            found_at=found,
            intent_implied=True,
        ))
    return leads


class FreelanceRuSource(Source):
    name = "freelance.ru"
    human = True

    def __init__(self, queries: list[str], interval: int, proxy: str | None):
        self.queries = queries
        self.interval = interval
        self.fetcher = Fetcher(proxy, pause=(4, 10))

    async def poll(self) -> list[Lead]:
        leads: list[Lead] = []
        for q in self.queries:
            r = await self.fetcher.get(URL, params={"q": q})
            r.raise_for_status()
            leads += parse_freelance_ru_html(r.text)
        return leads
