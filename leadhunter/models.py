from dataclasses import dataclass, field
from datetime import datetime, timezone


# Источник → (настройка-выключатель группы, значение по умолчанию). Группа выключена — источник
# не опрашивается, а его лиды не проходят конвейер.
SOURCE_SETTINGS = {
    "kwork": ("leads_boards", "1"), "rss": ("leads_boards", "1"), "weblancer": ("leads_boards", "1"),
    "freelance.ru": ("leads_boards", "1"),
    "telegram": ("leads_social", "1"), "vk": ("leads_social", "1"), "threads": ("leads_social", "1"),
    "instagram": ("leads_social", "1"),
    "hh.ru": ("leads_jobs", "0"),
    "yandex_maps": ("leads_maps", "1"), "site_audit": ("leads_maps", "1"),
    "web": ("leads_web", "0"),
}
SOURCE_GROUP_TITLES = {"leads_boards": "Заказы с бирж", "leads_social": "Запросы из соцсетей и чатов",
                       "leads_jobs": "Вакансии hh.ru", "leads_maps": "Компании с карт и аудит сайтов",
                       "leads_web": "Результаты поисковиков"}


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
        """Текст для оценки, правил, гео и дедупа. Название чата сюда не входит — оно в extra["chat_title"]."""
        return f"{self.title}\n{self.text}".strip()
