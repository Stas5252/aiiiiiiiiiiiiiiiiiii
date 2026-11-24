import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

SYSTEM = """Ты — ассистент по продажам исполнителя (фрилансера или студии). Бриф исполнителя:
{profile}

Задача: по тексту из чата/соцсети/биржи решить, ЛИД это или нет.
Лид — человек или компания, которые хотят ЗАКАЗАТЬ услугу из брифа (или бизнес, которому она явно нужна).
НЕ лид: предложения услуг от исполнителей и студий, резюме, реклама, курсы, обсуждения без запроса,
вакансии в штат на полный день, запросы вне услуг исполнителя.
Бюджет на is_lead не влияет: is_lead — только про то, хочет ли автор заказать услугу исполнителя.
Маленький бюджет, даже несоразмерный задаче или ниже цен «от» из брифа, — всё равно лид, просто с низким score:
порог по сумме проверяет программа, а не ты.
Если в брифе есть строка «Что не берёте» — заказ на такую тему не лид. Если есть строка «География» —
заказ, где работать нужно лично в городе, которого там нет, не лид.
{examples}
Ответь строго JSON без пояснений, поля в этом порядке:
{{"summary": "одна строка: кто и что хочет",
 "service": "сайт|бот|автоматизация|мультик|другое",
 "budget": "сумма из текста или null",
 "budget_rub": бюджет всего заказа в рублях одним числом (у вилки и «до …» — верхняя граница); null, если бюджет не назван, назван только снизу («от …»), не в рублях или это ставка за час, месяц или штуку,
 "why": "одна строка: чем этот заказ подходит исполнителю — услуга, бюджет, город, похожий кейс — и что в нём смущает, если есть; для не лида — почему не подходит",
 "is_lead": true|false, "score": 0-10, "urgency": "низкая|средняя|высокая",
 "reply_draft": "первое сообщение клиенту от лица исполнителя: 2-4 предложения, по делу, без воды, в тоне из брифа. Если в брифе есть кейс по этой теме — упомяни его одной фразой; если есть цена «от» на эту услугу — назови её; нет такой цены в брифе — никаких сумм не называй. Если бюджет заказчика ниже цены из брифа — не отказывай, не поучай и не оценивай его бюджет: назови цену и предложи, что можно сделать за его сумму, или спроси, что в задаче главное. Ровно один уточняющий вопрос. В конце — контакт из брифа; если контакта в брифе нет, никакого не называй. О себе пиши так, как сказано в брифе; если не ясно, мужчина это, женщина или команда, — без рода: «сделаю», «могу». Кейсы, цены, контакты и факты, которых нет в брифе, не выдумывай"}}"""


@dataclass
class Verdict:
    is_lead: bool
    score: int
    service: str = ""
    summary: str = ""
    urgency: str = ""
    budget: str = ""
    reply_draft: str = ""
    why: str = ""
    budget_rub: int | None = None  # бюджет всего заказа в рублях, как его понял ИИ; None — не назван


_TRUE = {"true", "yes", "y", "да", "1", "лид"}
_FALSE = {"false", "no", "n", "нет", "0", "не лид", "null", "none", ""}


def parse_bool(value) -> bool:
    """«false» и «нет» строкой — это «нет», а не непустая строка. Непонятный ответ считаем отказом."""
    if isinstance(value, str):
        v = value.strip().strip(".!\"'").lower()
        if v in _TRUE:
            return True
        if v not in _FALSE:
            log.warning("LLM: непонятное значение is_lead=%r, считаю «нет»", value)
        return False
    return bool(value)


def parse_rub(value) -> int | None:
    """15000, "15 000", "до 15 000 ₽" → 15000. null, текст без числа, ноль — None: сумму не знаем."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        m = re.search(r"\d[\d\s\u00a0]*", value)
        value = re.sub(r"\D", "", m.group()) if m else ""
    try:
        amount = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return amount if 0 < amount < 10 ** 9 else None


def parse_score(value) -> int:
    """7, "7", "8/10", 7.6 → целое 0..10."""
    if isinstance(value, str):
        m = re.search(r"\d+(?:[.,]\d+)?", value)
        value = float(m.group().replace(",", ".")) if m else 0
    try:
        return max(0, min(10, int(round(float(value or 0)))))
    except (TypeError, ValueError):
        return 0


class LLMQuotaError(RuntimeError):
    """Часовой лимит вызовов ИИ исчерпан."""


def _strip_fence(s: str) -> str:
    """Некоторые модели оборачивают JSON в ```json … ``` даже при response_format."""
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        s = s.rsplit("```", 1)[0]
    return s.strip()


def build_examples(good: list[str], bad: list[str]) -> str:
    """Отметки владельца («горячий» / «не лид») становятся few-shot примерами — ИИ подстраивается под вкус."""
    parts = []
    if good:
        parts.append("Примеры, которые владелец отметил как ХОРОШИЕ лиды:\n" + "\n".join(f"- {t[:300]}" for t in good))
    if bad:
        parts.append("Примеры, которые владелец отметил как НЕ ЛИД:\n" + "\n".join(f"- {t[:300]}" for t in bad))
    return "\n\n".join(parts)


class LLMClassifier:
    def __init__(self, api_key: str, base_url: str, model: str, max_per_hour: int = 300, fallback_model: str = ""):
        self.model = model
        self.fallback_model = fallback_model  # на 429/503 основной модели повторяем запрос на ней
        self.max_per_hour = max_per_hour
        self._calls: deque[float] = deque()
        self._refused = False
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}, timeout=120
        )

    def _allowed(self) -> bool:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 3600:
            self._calls.popleft()
        if len(self._calls) >= self.max_per_hour:
            self._refused = True
            return False
        self._refused = False
        self._calls.append(now)
        return True

    @property
    def quota_exhausted(self) -> bool:
        """Последний запрос не ушёл из-за часового лимита и лимит ещё действует."""
        if not self._refused:
            return False
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 3600:
            self._calls.popleft()
        self._refused = len(self._calls) >= self.max_per_hour
        return self._refused

    def quota_resets_in(self) -> int:
        """Через сколько минут освободится первый вызов."""
        if not self._calls:
            return 0
        return max(1, int((3600 - (time.monotonic() - self._calls[0])) // 60) + 1)

    async def complete_json(self, system: str, user: str, *, temperature: float = 0.7,
                            max_tokens: int | None = None) -> dict:
        """Любой запрос «верни JSON» — на нём работают контент-фабрика и генерация фраз. Бросает исключение при ошибке."""
        if not self._allowed():
            raise LLMQuotaError(f"лимит {self.max_per_hour} вызовов ИИ в час исчерпан")
        payload = {
            "model": self.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user[:12000]}],
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        r = await self.client.post("/chat/completions", json=payload)
        if r.status_code in (429, 500, 503) and self.fallback_model and self.fallback_model != self.model:
            log.warning("LLM %s: HTTP %s, повторяю на %s", self.model, r.status_code, self.fallback_model)
            payload["model"] = self.fallback_model
            r = await self.client.post("/chat/completions", json=payload)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(_strip_fence(content))

    async def classify(self, text: str, profile: str, good: list[str] = (), bad: list[str] = ()) -> Verdict | None:
        system = SYSTEM.format(profile=profile, examples=build_examples(list(good), list(bad)))
        try:
            data = await self.complete_json(system, text[:4000], temperature=0)
            return Verdict(
                is_lead=parse_bool(data.get("is_lead")),
                score=parse_score(data.get("score")),
                service=str(data.get("service") or ""),
                summary=str(data.get("summary") or ""),
                urgency=str(data.get("urgency") or ""),
                budget=str(data.get("budget") or "") if data.get("budget") else "",
                reply_draft=str(data.get("reply_draft") or ""),
                why=str(data.get("why") or "")[:300],
                budget_rub=parse_rub(data.get("budget_rub")),
            )
        except LLMQuotaError as e:
            log.warning("LLM: %s — лид оценивается по словарю", e)
            return None
        except Exception:
            # Ошибка ИИ не должна терять лид — пайплайн откатится на ключевые слова
            log.exception("LLM classification failed")
            return None
