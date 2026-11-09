import re
from dataclasses import dataclass, field

# Храним основы слов: "сайт" ловит "сайта", "сайты", "сайтик"
DEFAULT_TOPIC = [
    "сайт", "лендинг", "landing", "интернет-магазин", "веб", "web", "тильд", "tilda", "wordpress",
    "битрикс", "бот", "chatbot", "чат-бот", "автоматизац", "нейросет", "нейронк", "ии", "ai", "chatgpt",
    "gpt", "мультик", "мультфильм", "анимац", "аватар", "воронк", "crm", "amocrm", "амо", "интеграц",
    "парсер", "парсинг", "ролик", "видео",
]
DEFAULT_INTENT = [
    "ищу", "ищем", "нужен", "нужна", "нужно", "требуется", "кто сделает", "кто может сделать",
    "кто занимается", "посоветуйте", "порекомендуйте", "подскажите", "сколько стоит", "заказать",
    "подрядчик", "бюджет", "есть тз", "срочно",
]
# Чужие офферы, резюме и реклама — это не клиенты
DEFAULT_NEGATIVE = [
    "сделаю", "разработаю", "создам", "настрою", "предлагаю услуг", "наша студия", "наша команда",
    "мы делаем", "резюме", "ищу работу", "ищу заказы", "портфолио", "возьму заказ", "курс", "обучени",
    "вебинар", "скидк", "акция", "вакансия в", "рассылк",
]


@dataclass
class Keywords:
    topic: list[str] = field(default_factory=lambda: list(DEFAULT_TOPIC))
    intent: list[str] = field(default_factory=lambda: list(DEFAULT_INTENT))
    negative: list[str] = field(default_factory=lambda: list(DEFAULT_NEGATIVE))


@dataclass
class ScoreResult:
    score: int
    topic: list[str] = field(default_factory=list)
    intent: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def _pattern(stem: str) -> re.Pattern:
    stem = normalize(stem)
    # Короткие основы ("ии", "ai") только как целое слово, иначе ловим мусор
    tail = r"\b" if len(stem) <= 2 else r"\w*"
    return re.compile(r"(?<!\w)" + re.escape(stem) + tail)


def find_matches(text: str, stems: list[str]) -> list[str]:
    return [s for s in stems if _pattern(s).search(text)]


def score_text(text: str, kw: Keywords, intent_implied: bool = False) -> ScoreResult:
    norm = normalize(text)
    topic = find_matches(norm, kw.topic)
    intent = find_matches(norm, kw.intent)
    negative = find_matches(norm, kw.negative)
    if not topic:
        return ScoreResult(0, topic, intent, negative)
    intent_hits = max(len(intent), 1 if intent_implied else 0)
    score = min(len(topic), 3) * 2 + min(intent_hits, 2) * 3 - len(negative) * 3
    return ScoreResult(max(score, 0), topic, intent, negative)
