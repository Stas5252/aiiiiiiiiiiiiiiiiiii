import logging
from typing import Protocol

from .db import Database
from .llm import LLMClassifier
from .models import Lead
from .scoring import ScoreResult, score_text

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def notify(self, lead_id: int, lead: Lead, score: int, res: ScoreResult, summary: str | None) -> None: ...


class Pipeline:
    """Дедуп → скоринг → (ИИ-проверка) → сохранение → уведомление."""

    def __init__(self, db: Database, notifier: Notifier, default_threshold: int, llm: LLMClassifier | None = None):
        self.db = db
        self.notifier = notifier
        self.default_threshold = default_threshold
        self.llm = llm

    async def threshold(self) -> int:
        return int(await self.db.get_setting("threshold", str(self.default_threshold)))

    async def handle(self, lead: Lead) -> bool:
        if not await self.db.mark_seen(lead.source, lead.external_id):
            return False
        if not lead.text.strip() and not lead.title.strip():
            return False
        if not await self.db.mark_hash_seen(lead.full_text):
            return False

        kw = await self.db.get_keywords()
        res = score_text(lead.full_text, kw, intent_implied=lead.intent_implied)
        score = lead.preset_score if lead.preset_score is not None else res.score
        if score < await self.threshold():
            return False

        summary = None
        if self.llm and lead.preset_score is None:
            verdict = await self.llm.classify(lead.full_text)
            if verdict:
                if not verdict.is_lead:
                    log.info("LLM rejected %s/%s", lead.source, lead.external_id)
                    return False
                score, summary = verdict.score, f"{verdict.service}: {verdict.summary}"

        lead_id = await self.db.save_lead(lead, score, res, summary)
        if await self.db.get_setting("paused") != "1":
            await self.notifier.notify(lead_id, lead, score, res, summary)
        return True
