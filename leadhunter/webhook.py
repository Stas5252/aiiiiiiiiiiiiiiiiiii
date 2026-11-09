import logging

import httpx

from .models import Lead
from .pipeline import Enriched

log = logging.getLogger(__name__)


class WebhookNotifier:
    """POST JSON с каждым лидом — для n8n / Make / Google Sheets / amoCRM."""

    def __init__(self, url: str):
        self.url = url

    async def notify(self, lead_id: int, lead: Lead, info: Enriched) -> None:
        payload = {
            "id": lead_id, "source": lead.source, "score": info.score, "service": info.service,
            "urgency": info.urgency, "budget": info.budget, "summary": info.summary, "draft": info.draft,
            "title": lead.title, "text": lead.text, "author": lead.author, "contact": lead.contact,
            "url": lead.url, "found_at": lead.found_at.isoformat(),
            "matched": {"topic": info.res.topic, "intent": info.res.intent},
        }
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(self.url, json=payload)
                r.raise_for_status()
        except Exception as e:
            log.warning("webhook failed for lead %s: %s", lead_id, e)


class MultiNotifier:
    def __init__(self, *notifiers):
        self.notifiers = [n for n in notifiers if n]

    async def notify(self, lead_id: int, lead: Lead, info: Enriched) -> None:
        for n in self.notifiers:
            await n.notify(lead_id, lead, info)
