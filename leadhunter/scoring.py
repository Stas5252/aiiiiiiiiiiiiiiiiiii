import re
from dataclasses import dataclass, field
from functools import lru_cache

from .filters import _morph, tokens

# Слова по услугам. Словарное слово ловит свои формы («сайт» — «сайта», «сайты»), обрезанная основа
# («автоматизац») — всё, что с неё начинается. Подробнее — _word_rule().
TOPIC_CATEGORIES: dict[str, list[str]] = {
    "сайт": ["сайт", "лендинг", "landing", "интернет-магазин", "веб", "web", "тильд", "tilda", "wordpress",
             "битрикс", "квиз", "сайтик", "вебсайт", "website"],
    "бот": ["бот", "chatbot", "чат-бот", "телеграм-бот", "telegram-бот"],
    "автоматизация": ["автоматизац", "нейросет", "нейронк", "ии", "ai", "chatgpt", "gpt", "воронк", "crm",
                      "amocrm", "амо", "интеграц", "парсер", "парсинг", "n8n", "ии-агент", "ai-агент"],
    "мультик": ["мультик", "мультфильм", "анимац", "аватар", "ролик", "видеоролик", "рилс", "reels", "sora", "veo"],
}
DEFAULT_TOPIC: dict[str, str] = {w: cat for cat, words in TOPIC_CATEGORIES.items() for w in words}
DEFAULT_INTENT = [
    "ищу", "ищем", "нужен", "нужна", "нужно", "нужны", "требуется", "требуются", "необходим", "кто сделает", "кто может сделать",
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


_PLAIN_RE = re.compile(r"[а-яa-z0-9\s-]+")
_EXACT_POS = {"VERB", "INFN", "GRND", "PRTF", "PRTS"}


@lru_cache(maxsize=50_000)
def _normal_forms(token: str) -> frozenset[str]:
    return frozenset(p.normal_form.replace("ё", "е") for p in _morph.parse(token))


@lru_cache(maxsize=5_000)
def _word_rule(word: str) -> tuple[str, str]:
    """Как искать одно слово ключа: ("exact"|"lemma"|"prefix", с чем сравнивать).
    Словарное слово сравнивается по лемме («бот» = «боты», «ботов», но не «ботинки» и не «ботокс»);
    глагол — только в своей форме («сделаю» ≠ «сделает», «ищу» ≠ «ищете»); обрезанная основа
    («автоматизац», «тильд») и латиница — как начало слова; слова до 3 букв — только целиком."""
    if len(word) <= 2:
        return "exact", word
    if not _morph.word_is_known(word):
        return ("prefix", word) if len(word) >= 4 else ("exact", word)
    parses = _morph.parse(word)
    if any(p.tag.POS in _EXACT_POS for p in parses):  # «настрою» — это и «настроить», и «настрой»
        return "exact", word
    return "lemma", parses[0].normal_form.replace("ё", "е")


def _word_matches(rule: tuple[str, str], word: str, token: str) -> bool:
    kind, value = rule
    if token == word:
        return True
    if kind == "prefix":
        return token.startswith(value)
    if kind == "lemma":
        return value in _normal_forms(token)
    # короткая латиница с номером версии: «gpt4», «veo3»
    return len(value) >= 3 and token.startswith(value) and token[len(value):].isdigit()


def _find_span(key: str, toks: list[str], norm: str) -> tuple[int, int] | None:
    """Где в тексте встретился ключ: (номер первого токена, длина в токенах)."""
    key = normalize(key)
    if not _PLAIN_RE.fullmatch(key):  # ключ со знаками («кейс:», «c++») — ищем как есть
        return (-1, 0) if _pattern(key).search(norm) else None
    words = tokens(key)
    if not words:
        return None
    rules = [_word_rule(w) for w in words]
    n = len(rules)
    for i in range(len(toks) - n + 1):
        if all(_word_matches(r, words[k], toks[i + k]) for k, r in enumerate(rules)):
            return i, n
    return None


def find_matches(text: str, stems) -> list[str]:
    """Ключи, найденные в тексте, в порядке словаря. Одно и то же слово текста засчитывается один раз:
    «нужны» не даёт три совпадения по ключам «нужен», «нужна», «нужно»."""
    norm = normalize(text)
    toks = tokens(norm)
    spans = {s: sp for s in stems if (sp := _find_span(s, toks, norm))}
    taken: set[int] = set()
    kept = set()
    for s in sorted(spans, key=lambda s: -spans[s][1]):  # длинные фразы важнее входящих в них слов
        start, n = spans[s]
        cells = set(range(start, start + n))
        if cells & taken:
            continue
        taken |= cells
        kept.add(s)
    return [s for s in stems if s in kept]


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
