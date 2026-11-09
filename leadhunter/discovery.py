"""Автопоиск чатов: ищем по запросам ниши, читаем последние сообщения, оцениваем «есть ли тут заказчики»,
лучшие — вступаем (с дневным лимитом, чтобы аккаунт не выглядел как бот)."""
import asyncio
import json
import logging
from datetime import datetime

from .chat_catalog import DEMAND_MARKERS, NICHES, SUPPLY_MARKERS
from .db import Database
from .filters import lemmatize
from .llm import LLMClassifier
from .scoring import DEFAULT_TOPIC

log = logging.getLogger(__name__)
_TOPIC_LEMMAS = {lemmatize(w)[0] for w in DEFAULT_TOPIC if lemmatize(w)}
_DEMAND = [lemmatize(m) for m in DEMAND_MARKERS]
_SUPPLY = [lemmatize(m) for m in SUPPLY_MARKERS]


def _has(lem: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(lem[i:i + n] == phrase for i in range(len(lem) - n + 1))


def score_messages(messages: list[str]) -> int:
    """0–100: доля сообщений со спросом и по нашей теме минус доля офферов исполнителей."""
    if not messages:
        return 0
    demand = supply = topic = 0
    for text in messages:
        lem = lemmatize(text)
        if any(_has(lem, d) for d in _DEMAND):
            demand += 1
        if any(_has(lem, s) for s in _SUPPLY):
            supply += 1
        if any(l in _TOPIC_LEMMAS for l in lem):
            topic += 1
    n = len(messages)
    raw = (demand * 60 + topic * 50 - supply * 40) / n
    return max(0, min(100, int(raw)))


LLM_PROMPT = """Ниже последние сообщения Telegram-чата «{title}». Студия ищет чаты, где потенциальные клиенты
ПРОСЯТ сделать сайт / бота / автоматизацию / видео — а не чаты, где исполнители рекламируют себя.
Оцени от 0 до 100, насколько этот чат полезен как источник клиентов. Ответь JSON: {{"score": N, "why": "..."}}

{messages}"""


class ChatDiscovery:
    def __init__(self, tg, db: Database, llm: LLMClassifier | None, join_per_day: int = 5):
        self.tg = tg
        self.db = db
        self.llm = llm
        self.join_per_day = join_per_day

    async def _llm_score(self, title: str, messages: list[str]) -> int | None:
        if not self.llm:
            return None
        try:
            r = await self.llm.client.post("/chat/completions", json={
                "model": self.llm.model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": LLM_PROMPT.format(
                    title=title, messages="\n".join(f"- {m[:200]}" for m in messages[:40]))}],
            })
            r.raise_for_status()
            return int(json.loads(r.json()["choices"][0]["message"]["content"]).get("score", 0))
        except Exception:
            log.debug("llm chat score failed", exc_info=True)
            return None

    async def _joined_today(self) -> int:
        return int(await self.db.get_setting(f"discover_joined:{datetime.now().date()}", "0"))

    async def run(self, niche: str, auto_join: bool = True) -> list[dict]:
        queries = NICHES.get(niche.lower()) or [niche]
        known = {str(c["id"]) for c in await self.tg.list_chats()}
        seen: dict[str, dict] = {}
        for q in queries:
            for c in await self.tg.find_chats(q, limit=10):
                if c["username"] and c["is_group"] and c["username"] not in seen:
                    seen[c["username"]] = c
            await asyncio.sleep(2)
        results = []
        for username, c in list(seen.items())[:25]:
            try:
                msgs = await self.tg.recent_messages(username, 50)
            except Exception as e:
                log.info("discover: %s недоступен: %s", username, e)
                continue
            score = score_messages(msgs)
            llm_score = await self._llm_score(c["title"], msgs)
            if llm_score is not None:
                score = (score + llm_score) // 2
            results.append({**c, "score": score, "joined": False})
            await asyncio.sleep(3)
        results.sort(key=lambda x: -x["score"])
        if auto_join:
            for c in results:
                if c["score"] < 40 or await self._joined_today() >= self.join_per_day:
                    break
                if str(c.get("id")) in known:
                    continue
                try:
                    await self.tg.join(c["username"])
                    c["joined"] = True
                    await self.db.set_setting(f"discover_joined:{datetime.now().date()}", str(await self._joined_today() + 1))
                    await asyncio.sleep(30)  # живой человек не вступает в 5 чатов за секунду
                except Exception as e:
                    log.info("discover: не вступил в %s: %s", c["username"], e)
        return results
