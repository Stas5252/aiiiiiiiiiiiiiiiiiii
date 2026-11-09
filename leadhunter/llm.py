import json
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

PROMPT = """Ты фильтруешь входящие сообщения для студии, которая делает: сайты и лендинги, Telegram-ботов и
ИИ-автоматизацию бизнеса, ИИ-мультики и видеоролики. Лид — это человек или компания, которые ХОТЯТ
ЗАКАЗАТЬ такую услугу (или бизнес, которому она явно нужна). Не лид: чужие предложения услуг, резюме,
реклама, курсы, обсуждения без запроса.
Ответь только JSON: {"is_lead": true/false, "score": 0-10, "service": "сайт|бот|автоматизация|мультик|другое",
"summary": "одна строка — что нужно клиенту"}"""


@dataclass
class Verdict:
    is_lead: bool
    score: int
    service: str
    summary: str


class LLMClassifier:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    async def classify(self, text: str) -> Verdict | None:
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": text[:4000]}],
        }
        try:
            r = await self.client.post("/chat/completions", json=payload)
            r.raise_for_status()
            data = json.loads(r.json()["choices"][0]["message"]["content"])
            return Verdict(
                is_lead=bool(data.get("is_lead")),
                score=max(0, min(10, int(data.get("score", 0)))),
                service=str(data.get("service", "")),
                summary=str(data.get("summary", "")),
            )
        except Exception:
            # Ошибка ИИ не должна терять лид — пайплайн откатится на ключевые слова
            log.exception("LLM classification failed")
            return None
