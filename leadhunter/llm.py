import json
import logging
import time
from collections import deque
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

SYSTEM = """Ты — ассистент по продажам в студии. Профиль студии: {profile}

Задача: по тексту из чата/соцсети/биржи решить, ЛИД это или нет.
Лид — человек или компания, которые хотят ЗАКАЗАТЬ услугу из профиля студии (или бизнес, которому она явно нужна).
НЕ лид: предложения услуг от исполнителей и студий, резюме, реклама, курсы, обсуждения без запроса,
вакансии в штат на полный день, запросы вне профиля студии.
{examples}
Ответь строго JSON без пояснений:
{{"is_lead": true|false, "score": 0-10, "service": "сайт|бот|автоматизация|мультик|другое",
 "urgency": "низкая|средняя|высокая", "budget": "сумма из текста или null",
 "summary": "одна строка: кто и что хочет",
 "reply_draft": "первое сообщение клиенту от лица студии: 2-3 предложения, по делу, без воды, с одним уточняющим вопросом"}}"""


@dataclass
class Verdict:
    is_lead: bool
    score: int
    service: str = ""
    summary: str = ""
    urgency: str = ""
    budget: str = ""
    reply_draft: str = ""


def build_examples(good: list[str], bad: list[str]) -> str:
    """Отметки владельца («горячий» / «не лид») становятся few-shot примерами — ИИ подстраивается под вкус."""
    parts = []
    if good:
        parts.append("Примеры, которые владелец отметил как ХОРОШИЕ лиды:\n" + "\n".join(f"- {t[:300]}" for t in good))
    if bad:
        parts.append("Примеры, которые владелец отметил как НЕ ЛИД:\n" + "\n".join(f"- {t[:300]}" for t in bad))
    return "\n\n".join(parts)


class LLMClassifier:
    def __init__(self, api_key: str, base_url: str, model: str, max_per_hour: int = 300):
        self.model = model
        self.max_per_hour = max_per_hour
        self._calls: deque[float] = deque()
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    def _allowed(self) -> bool:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 3600:
            self._calls.popleft()
        if len(self._calls) >= self.max_per_hour:
            return False
        self._calls.append(now)
        return True

    async def classify(self, text: str, profile: str, good: list[str] = (), bad: list[str] = ()) -> Verdict | None:
        if not self._allowed():
            log.warning("LLM: лимит %d вызовов/час исчерпан, работаем по словарю", self.max_per_hour)
            return None
        system = SYSTEM.format(profile=profile, examples=build_examples(list(good), list(bad)))
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text[:4000]}],
        }
        try:
            r = await self.client.post("/chat/completions", json=payload)
            r.raise_for_status()
            data = json.loads(r.json()["choices"][0]["message"]["content"])
            return Verdict(
                is_lead=bool(data.get("is_lead")),
                score=max(0, min(10, int(data.get("score") or 0))),
                service=str(data.get("service") or ""),
                summary=str(data.get("summary") or ""),
                urgency=str(data.get("urgency") or ""),
                budget=str(data.get("budget") or "") if data.get("budget") else "",
                reply_draft=str(data.get("reply_draft") or ""),
            )
        except Exception:
            # Ошибка ИИ не должна терять лид — пайплайн откатится на ключевые слова
            log.exception("LLM classification failed")
            return None
