import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .db import Database
from .extract import extract_budget, extract_contacts
from .llm import LLMClassifier
from .models import Lead
from .scoring import ScoreResult, score_text

log = logging.getLogger(__name__)


@dataclass
class Enriched:
    score: int
    res: ScoreResult
    summary: str | None = None
    service: str = ""
    budget: str = ""
    urgency: str = ""
    draft: str = ""


class Notifier(Protocol):
    async def notify(self, lead_id: int, lead: Lead, info: Enriched) -> None: ...


class Pipeline:
    """Дедуп → игнор-листы → свежесть → извлечение контактов → словарный скоринг → ИИ → сохранение → уведомление."""

    def __init__(self, db: Database, notifier: Notifier, default_threshold: int, llm: LLMClassifier | None = None,
                 *, max_age_hours: int = 48, llm_min_score: int = 2, profile: str = ""):
        self.db = db
        self.notifier = notifier
        self.default_threshold = default_threshold
        self.llm = llm
        self.max_age = timedelta(hours=max_age_hours)
        self.llm_min_score = llm_min_score
        self.default_profile = profile

    async def threshold(self) -> int:
        return int(await self.db.get_setting("threshold", str(self.default_threshold)))

    async def _is_ignored(self, lead: Lead) -> bool:
        ignored = await self.db.get_ignored()
        chat_key = str(lead.extra.get("chat_key", "")).lower()
        if chat_key and chat_key in ignored["chat"]:
            return True
        author_keys = {lead.author.lower(), lead.contact.lower()} - {""}
        return bool(author_keys & ignored["author"].keys())

    async def handle(self, lead: Lead) -> bool:
        if not await self.db.mark_seen(lead.source, lead.external_id):
            return False
        if len(lead.full_text) < 15:
            return False
        if not await self.db.mark_hash_seen(lead.full_text):
            return False
        if await self._is_ignored(lead):
            return False
        found = lead.found_at if lead.found_at.tzinfo else lead.found_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - found > self.max_age:
            return False

        if not lead.contact:
            lead.contact = extract_contacts(lead.text)

        info = await self._enrich(lead)
        if info is None or info.score < await self.threshold():
            return False

        lead_id = await self.db.save_lead(lead, info.score, info.res, summary=info.summary, service=info.service,
                                          budget=info.budget, urgency=info.urgency, draft=info.draft)
        if await self.db.get_setting("paused") != "1":
            await self.notifier.notify(lead_id, lead, info)
        return True

    async def _enrich(self, lead: Lead) -> Enriched | None:
        kw = await self.db.get_keywords()
        res = score_text(lead.full_text, kw, intent_implied=lead.intent_implied)
        service = res.services[0] if res.services else ""
        budget = extract_budget(lead.full_text)

        if lead.preset_score is not None:
            return Enriched(lead.preset_score, res, service=service or "сайт", budget=budget)
        if not self.llm:
            return Enriched(res.score, res, service=service, budget=budget)

        # ИИ смотрит всё, где есть хотя бы слово по теме, — ловим лиды без явного «ищу/нужен»
        if res.score < self.llm_min_score:
            return None
        profile = await self.db.get_setting("profile", self.default_profile) or self.default_profile
        good = [r["text"] for r in await self.db.leads_by_status("hot", 5)]
        bad = [r["text"] for r in await self.db.leads_by_status("spam", 5)]
        verdict = await self.llm.classify(lead.full_text, profile, good, bad)
        if verdict is None:
            return Enriched(res.score, res, service=service, budget=budget)
        if not verdict.is_lead:
            log.info("LLM отклонил %s/%s: %s", lead.source, lead.external_id, verdict.summary)
            return None
        return Enriched(verdict.score, res, summary=verdict.summary, service=verdict.service or service,
                        budget=verdict.budget or budget, urgency=verdict.urgency, draft=verdict.reply_draft)
