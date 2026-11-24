import html
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from . import brief
from .db import Database
from .dedup import NearDuplicateIndex
from .extract import extract_budget, extract_contacts
from .filters import evaluate
from .freshness import Freshness, check as freshness_check
from .geo import geo_allows
from .llm import LLMClassifier
from .models import SOURCE_SETTINGS, Lead
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
    rule: str = ""
    author_bio: str = ""
    freshness: str = ""  # «12 мин назад · 2 откл.» для бирж
    why: str = ""  # почему лид подходит под бриф — одна строка от ИИ


class Notifier(Protocol):
    async def notify(self, lead_id: int, lead: Lead, info: Enriched) -> bool | None: ...  # True — доставлено


# Заявка, оставленная человеком на нашем лендинге: это уже клиент, а не кандидат в лиды
DIRECT_SOURCES = {"landing"}
SPAM_IGNORE_HOURS = 24
WEB_INTENT_RE = re.compile(r"\b(?:ищу|ищем|требуется|требуются|закажу|закажем|нужен исполнитель|нужен подрядчик)\b", re.I)


class Pipeline:
    """Дедуп → игнор-листы → свежесть → гео → правила → контакты → словарь → ИИ → порог → спам-детект →
    сохранение → уведомление. Заявки с лендинга идут в обход фильтров: _handle_direct()."""

    def __init__(self, db: Database, notifier: Notifier, default_threshold: int, llm: LLMClassifier | None = None,
                 *, max_age_hours: int = 48, llm_min_score: int = 2, profile: str = "", spam_chats: int = 3):
        self.db = db
        self.notifier = notifier
        self.default_threshold = default_threshold
        self.llm = llm
        self.max_age = timedelta(hours=max_age_hours)
        self.llm_min_score = llm_min_score
        self.default_profile = profile
        self.spam_chats = spam_chats
        self.near_dups = NearDuplicateIndex()
        self._warmed = False
        self._quota_alert_at = 0.0
        # Необязательный хук: по sender_id Telegram вернуть bio автора — идёт в ИИ и карточку
        self.author_bio: Callable[[int], Awaitable[str]] | None = None

    async def threshold(self) -> int:
        return int(await self.db.get_setting("threshold", str(self.default_threshold)))

    async def _is_ignored(self, lead: Lead) -> bool:
        ignored = await self.db.get_ignored()
        chat_key = str(lead.extra.get("chat_key", "")).lower()
        if chat_key and chat_key in ignored["chat"]:
            return True
        author_keys = {lead.author.lower(), lead.contact.lower()} - {""}
        return bool(author_keys & ignored["author"].keys())

    async def _is_spammer(self, lead: Lead) -> bool:
        """Один автор с запросами в нескольких чатах за час — рассылка исполнителя, а не клиент.
        Считаются только сообщения, прошедшие оценку. Игнор временный, владелец получает алерт."""
        chat_key = lead.extra.get("chat_key")
        if not chat_key or not lead.contact or self.spam_chats <= 0:
            return False
        n = await self.db.author_seen(lead.contact, chat_key)
        if n < self.spam_chats:
            return False
        await self.db.add_ignored("author", lead.contact, f"авто: запросы в {n} чатах за час, на {SPAM_IGNORE_HOURS} ч",
                                  hours=SPAM_IGNORE_HOURS)
        await self.db.add_alert(
            f"spam:{lead.contact.lower()}",
            f"🚫 {html.escape(lead.contact)} оставил запросы в {n} чатах за час — похоже на рассылку. "
            f"Его сообщения не показываю {SPAM_IGNORE_HOURS} ч. Вернуть раньше: /unblock {html.escape(lead.contact)}")
        log.info("spammer %s: %d чатов за час", lead.contact, n)
        return True

    async def _warm(self) -> None:
        """После перезапуска семантический дедуп должен помнить лиды последних 48 ч."""
        if self._warmed:
            return
        self._warmed = True
        for text, at in await self.db.recent_lead_texts(int(self.near_dups.ttl // 3600)):
            self.near_dups.add(text, at.timestamp())

    async def _handle_direct(self, lead: Lead, quiet: bool) -> bool:
        """Заявка с лендинга: человек сам оставил контакт. Тексты заявок шаблонные, поэтому дедуп по тексту,
        правила, гео и порог к ним не применяются — повтор отсекает только уникальный external_id."""
        if not await self.db.mark_seen(lead.source, lead.external_id):
            return False
        if not lead.contact:
            lead.contact = extract_contacts(lead.text)
        res = score_text(lead.full_text, await self.db.get_keywords(), intent_implied=True)
        service = res.services[0] if res.services else ""
        score = lead.preset_score if lead.preset_score is not None else 9
        info = Enriched(score, res, service=service, budget=extract_budget(lead.text),
                        draft=await self._template(service))
        # pending даже на паузе: заявка дойдёт, как только паузу снимут
        lead_id = await self.db.save_lead(lead, info.score, info.res, service=info.service, budget=info.budget,
                                          draft=info.draft, pending=True)
        if not quiet and await self.db.get_setting("paused") != "1":
            await self.notifier.notify(lead_id, lead, info)
        return True

    async def handle(self, lead: Lead, *, quiet: bool = False) -> bool:
        """quiet — сохранить без уведомления (лимит пачки у источника)."""
        if lead.source in DIRECT_SOURCES:
            return await self._handle_direct(lead, quiet)
        if lead.source.startswith("web:") and not WEB_INTENT_RE.search(lead.full_text):
            return False
        setting = SOURCE_SETTINGS.get(lead.source.split(":")[0])
        if setting and await self.db.get_setting(*setting) != "1":
            return False
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
        geo = await self.db.get_setting("geo", "")
        if geo and not geo_allows(lead.full_text, [c.strip() for c in geo.split(",") if c.strip()]):
            return False

        rule, blocked = evaluate(lead.full_text, [r["expr"] for r in await self.db.rules()])
        if blocked:
            return False
        # Бриф: заказ с названной суммой ниже минимального не присылаем (правило владельца сильнее)
        if not rule and brief.below_min(lead.full_text, await brief.load(self.db)):
            log.debug("%s/%s пропущен: бюджет ниже минимального из брифа", lead.source, lead.external_id)
            return False
        # Биржи: заказ старше N часов или с толпой откликов уже не забрать — не шлём
        fresh = Freshness.from_settings({"board_max_age_hours": await self.db.get_setting("board_max_age_hours"),
                                         "max_competitors": await self.db.get_setting("max_competitors")})
        ok, bonus, mark = freshness_check(lead, fresh)
        if not ok:
            log.debug("%s/%s пропущен: %s", lead.source, lead.external_id, mark)
            return False
        if not lead.contact:
            lead.contact = extract_contacts(lead.text)

        info = await self._enrich(lead, rule)
        if info is None:
            return False
        info.freshness = mark
        if bonus and lead.preset_score is None:
            info.score = min(10, info.score + bonus)
        # Балл, который источник выставил сам (карты — 6, аудит сайта — 5), с порогом не сравниваем:
        # иначе /threshold 6–7 молча выключает карты целиком. Их выключатель — группа «Компании с карт».
        if lead.preset_score is None and info.score < await self.threshold():
            return False
        if await self._is_spammer(lead):
            return False
        await self._warm()
        if self.near_dups.is_duplicate(lead.full_text):
            return False

        deliver = not quiet and await self.db.get_setting("paused") != "1"
        lead_id = await self.db.save_lead(lead, info.score, info.res, summary=info.summary, service=info.service,
                                          budget=info.budget, urgency=info.urgency, draft=info.draft,
                                          rule=info.rule, author_bio=info.author_bio, freshness=info.freshness,
                                          why=info.why, pending=deliver)
        if deliver:
            await self.notifier.notify(lead_id, lead, info)
        return True

    async def _quota_alert(self) -> None:
        """Лимит ИИ исчерпан — владелец должен знать, что до конца часа в карточки может идти шум."""
        if time.monotonic() - self._quota_alert_at < 3600 and self._quota_alert_at:
            return
        self._quota_alert_at = time.monotonic()
        minutes = self.llm.quota_resets_in() if hasattr(self.llm, "quota_resets_in") else 60
        await self.db.add_alert(
            "llm_quota",
            f"⚠️ Лимит ИИ исчерпан ({getattr(self.llm, 'max_per_hour', '?')} вызовов в час). Примерно {minutes} мин "
            "лиды оцениваются только по словарю: в карточки может попасть реклама и предложения исполнителей, "
            "которые ИИ отсеивал. Лимит задаётся настройкой LLM_MAX_PER_HOUR.")

    async def _enrich(self, lead: Lead, rule: str | None) -> Enriched | None:
        kw = await self.db.get_keywords()
        res = score_text(lead.full_text, kw, intent_implied=lead.intent_implied)
        service = res.services[0] if res.services else ""
        budget = extract_budget(lead.full_text)
        threshold = await self.threshold()

        if lead.preset_score is not None:
            return Enriched(lead.preset_score, res, service=service or "сайт", budget=budget)

        base = max(res.score, threshold) if rule else res.score
        if not self.llm:
            return Enriched(base, res, service=service, budget=budget, rule=rule or "",
                            draft=await self._template(service))

        # ИИ смотрит всё, где есть хотя бы слово по теме или сработало правило
        if res.score < self.llm_min_score and not rule:
            return None
        bio = ""
        if self.author_bio and lead.source == "telegram" and lead.extra.get("sender_id"):
            try:
                bio = await self.author_bio(int(lead.extra["sender_id"]))
            except Exception:
                log.debug("bio fetch failed", exc_info=True)
        # Правило владельца сильнее минимального заказа из брифа: такому заказу ИИ минимум не показываем
        profile = await brief.profile(self.db, self.default_profile, with_min=not rule)
        good = [r["text"] for r in await self.db.leads_by_status("hot", 5)]
        bad = [r["text"] for r in await self.db.leads_by_status("spam", 5)]
        text = lead.full_text + (f"\n\n[Об авторе: {bio}]" if bio else "")
        verdict = await self.llm.classify(text, profile, good, bad)
        if verdict is None:
            if getattr(self.llm, "quota_exhausted", False):
                await self._quota_alert()
            return Enriched(base, res, service=service, budget=budget, rule=rule or "", author_bio=bio,
                            draft=await self._template(service))
        if not verdict.is_lead:
            log.info("LLM отклонил %s/%s: %s", lead.source, lead.external_id, verdict.summary)
            return None
        return Enriched(verdict.score, res, summary=verdict.summary, service=verdict.service or service,
                        budget=verdict.budget or budget, urgency=verdict.urgency, draft=verdict.reply_draft,
                        rule=rule or "", author_bio=bio, why=verdict.why)

    async def _template(self, service: str) -> str:
        return (await self.db.get_template(service) if service else None) or ""
