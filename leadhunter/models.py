from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Lead:
    source: str
    external_id: str
    text: str
    url: str = ""
    title: str = ""
    author: str = ""
    contact: str = ""
    found_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Источник, где каждая запись по определению заказ (биржа, вакансия): намерение не ищем в тексте
    intent_implied: bool = False
    # Источник сам знает, что это лид (например, бизнес без сайта) — ключевые слова и ИИ не нужны
    preset_score: int | None = None
    # Сколько исполнителей уже откликнулось (биржи). None = источник не знает
    competitors: int | None = None
    # chat_key/chat_title для Telegram — чтобы можно было заглушить шумный чат из карточки
    extra: dict = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return f"{self.title}\n{self.text}".strip()
