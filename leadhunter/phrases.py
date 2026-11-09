import json
import logging

from .config import Settings
from .db import Database
from .llm import LLMClassifier

log = logging.getLogger(__name__)
KEY = "search_phrases"

GEN_PROMPT = """Профиль студии: {profile}

Сгенерируй 15 коротких поисковых фраз (2-4 слова), которыми потенциальные клиенты пишут в соцсетях и чатах,
когда ищут такие услуги. Пиши как пишут живые люди: «нужен сайт», «кто делает ботов», «посоветуйте разработчика».
Без хэштегов, без кавычек. Ответь JSON: {{"phrases": ["...", "..."]}}"""


class PhraseProvider:
    """Фразы-намерения для Threads/VK/поисковиков: из базы (правятся через бота или ИИ) либо из .env."""

    def __init__(self, db: Database, defaults: list[str]):
        self.db = db
        self.defaults = defaults

    async def get(self) -> list[str]:
        raw = await self.db.get_setting(KEY)
        if raw:
            try:
                phrases = json.loads(raw)
                if phrases:
                    return phrases
            except json.JSONDecodeError:
                pass
        return self.defaults

    async def set(self, phrases: list[str]) -> None:
        clean = list(dict.fromkeys(p.strip().lower() for p in phrases if p.strip()))
        await self.db.set_setting(KEY, json.dumps(clean, ensure_ascii=False))

    async def generate(self, llm: LLMClassifier, profile: str) -> list[str]:
        r = await llm.client.post("/chat/completions", json={
            "model": llm.model, "temperature": 0.7, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": GEN_PROMPT.format(profile=profile)}],
        })
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        phrases = [str(p) for p in data.get("phrases", [])][:20]
        if phrases:
            await self.set(phrases)
        return phrases


def default_phrases(s: Settings) -> list[str]:
    return list(s.search_phrases)
