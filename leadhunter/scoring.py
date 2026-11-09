import re
from dataclasses import dataclass, field

# Основы слов по услугам: "сайт" ловит "сайта", "сайты", "сайтик"
TOPIC_CATEGORIES: dict[str, list[str]] = {
    "сайт": ["сайт", "лендинг", "landing", "интернет-магазин", "веб", "web", "тильд", "tilda", "wordpress",
             "битрикс", "квиз", "визитк"],
    "бот": ["бот", "chatbot", "чат-бот", "телеграм-бот", "telegram-бот"],
    "автоматизация": ["автоматизац", "нейросет", "нейронк", "ии", "ai", "chatgpt", "gpt", "воронк", "crm",
                      "amocrm", "амо", "интеграц", "парсер", "парсинг", "n8n", "ии-агент", "ai-агент"],
    "мультик": ["мультик", "мультфильм", "анимац", "аватар", "ролик", "видеоролик", "рилс", "reels", "sora", "veo"],
}
DEFAULT_TOPIC: dict[str, str] = {w: cat for cat, words in TOPIC_CATEGORIES.items() for w in words}
DEFAULT_INTENT = [
    "ищу", "ищем", "нужен", "нужна", "нужно", "требуется", "кто сделает", "кто может сделать",
    "кто занимается", "посоветуйте", "порекомендуйте", "подскажите", "сколько стоит", "заказать",
    "подрядчик", "бюджет", "есть тз", "срочно", "готов платить", "оплачу",
]
# Чужие офферы, резюме и реклама — это не клиенты
DEFAULT_NEGATIVE = [
    "сделаю", "разработаю", "создам", "настрою", "предлагаю услуг", "наша студия", "наша команда",
    "мы делаем", "резюме", "ищу работу", "ищу заказы", "ищу клиентов", "портфолио", "возьму заказ", "курс",
    "обучени", "вебинар", "скидк", "акция", "рассылк", "кейс:", "услуги по",
]


@dataclass
class Keywords:
    topic: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TOPIC))  # основа -> услуга
    intent: list[str] = field(default_factory=lambda: list(DEFAULT_INTENT))
    negative: list[str] = field(default_factory=lambda: list(DEFAULT_NEGATIVE))


@dataclass
class ScoreResult:
    score: int
    topic: list[str] = field(default_factory=list)
    intent: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def _pattern(stem: str) -> re.Pattern:
    stem = normalize(stem)
    # Короткие основы ("ии", "ai") только как целое слово, иначе ловим мусор
    tail = r"\b" if len(stem) <= 2 else r"\w*"
    return re.compile(r"(?<!\w)" + re.escape(stem) + tail)


def find_matches(text: str, stems) -> list[str]:
    return [s for s in stems if _pattern(s).search(text)]


def score_text(text: str, kw: Keywords, intent_implied: bool = False) -> ScoreResult:
    norm = normalize(text)
    topic = find_matches(norm, kw.topic)
    intent = find_matches(norm, kw.intent)
    negative = find_matches(norm, kw.negative)
    services = list(dict.fromkeys(kw.topic.get(t, "другое") for t in topic))
    if not topic:
        return ScoreResult(0, topic, intent, negative, services)
    intent_hits = max(len(intent), 1 if intent_implied else 0)
    score = min(len(topic), 3) * 2 + min(intent_hits, 2) * 3 - len(negative) * 3
    return ScoreResult(max(0, min(score, 10)), topic, intent, negative, services)
