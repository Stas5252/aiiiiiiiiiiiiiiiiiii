"""Проверка текста на выдумки. ИИ любит «70% клиентов» и «заявок стало втрое больше» — цифры, которых владелец
не называл. Программа находит в материале числа с единицами и сверяет их с брифом: чего в брифе нет, показывает
владельцу списком «проверьте». Ничего не вычёркивает и не запрещает — решает человек."""
import re

from .render import plain

THREADS_LIMIT = 500
_NUM = r"\d+(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_UNIT = (r"%|процент\w*|₽|руб\w*|р\.|тыс\w*|млн\w*|\$|€|раз(?:а|ов)?\b|[xх]\b"
         r"|дн(?:я|ей|ю|ям)\b|день|недел\w*|месяц\w*|час(?:а|ов)?\b|минут\w*|секунд\w*|сек\b|лет\b|год\w*"
         r"|клиент\w*|заказчик\w*|проект\w*|заяв\w*|сайт(?:а|ов)?\b|кейс\w*|звонк\w*|звонок|продаж\w*|брон\w*"
         r"|посетител\w*|пользовател\w*|человек\w*|люд\w+|компани\w*|гост\w+|покупател\w*|подписчик\w*|просмотр\w*")
_BETWEEN = r"(?:[  ]?(?:рабоч\w+|календарн\w+|нов\w+|первы\w+)[  ])?"
CLAIM = re.compile(rf"(?<![\w#@/.,:-])(?:[xх])?({_NUM})(?:\s*[–—-]\s*({_NUM}))?(\+)?[  ]?{_BETWEEN}({_UNIT})", re.I)
RATIO = re.compile(r"(?<![\w.,-])\d+\s+из\s+\d+(?![\w.,]*\d)", re.I)
WORDS = re.compile(r"\b(?:вдвое|втрое|вчетверо|впятеро|вдесятеро|в\s+(?:два|три|четыре|пять|шесть|семь|восемь|девять|десять|сто)\s+раз\w*"
                   r"|кажд\w+\s+(?:втор\w+|трет\w+|четв[её]рт\w+|пят\w+|десят\w+)|половин\w+\s+(?:клиент|посетител|пользовател|заяв|компани|люд)\w*"
                   r"|большинств\w+\s+(?:клиент|посетител|пользовател|компани|люд)\w*|по\s+статистике|исследовани\w+\s+показ\w+)", re.I)
WE = re.compile(r"(?<![\w-])(?:мы|наш(?:а|е|и|у|ей|его|ему|им|их|ими|ем)?|нашей|у\s+нас|к\s+нам|с\s+нами)(?![\w-])", re.I)
TEAM = re.compile(r"(?<![\w-])(?:мы|наш\w*|команд\w+|студи\w+|агентств\w+|компани\w+|сотрудник\w*)(?![\w-])", re.I)
WE_NOTE = "«мы» — по брифу вы работаете один"
_SKIP_KEYS = {"image_prompt", "hashtags", "color", "shape", "bg", "style", "id", "pos", "emotion", "action", "speaker",
              "prompt", "kind", "shot", "duration", "characters"}


def _norm(num: str) -> str:
    num = re.sub(r"[  ]", "", num).replace(",", ".")
    return num.rstrip("0").rstrip(".") if "." in num else num


def _scale(num: str, unit: str) -> str:
    """«15 тыс» и «15 000» — одно и то же число."""
    unit = unit.lower()
    mult = 1000 if unit.startswith("тыс") else 1_000_000 if unit.startswith("млн") else 1
    if mult == 1:
        return num
    return _norm(f"{float(num) * mult:.2f}")


def known_numbers(source: str) -> set[str]:
    """Все числа, которые владелец сам назвал в брифе."""
    out = set()
    # число внутри имени («N7», «@Butov52») — не цифра из брифа
    for m in re.finditer(rf"(?<![\w@])({_NUM})[  ]?(тыс\w*|млн\w*)?", source or "", re.I):
        num = _norm(m.group(1))
        out.add(num)
        if m.group(2):
            out.add(_scale(num, m.group(2)))
    return out


def solo(who: str) -> bool:
    """Исполнитель работает один: в поле брифа «Кто вы» нет «мы», «команда», «студия»."""
    return bool(who and who.strip()) and not TEAM.search(who)


def strings(data, key: str = "") -> list[str]:
    """Все тексты материала, которые увидит читатель (без технических полей)."""
    if key in _SKIP_KEYS:
        return []
    if isinstance(data, str):
        return [data] if data.strip() else []
    if isinstance(data, dict):
        return [s for k, v in data.items() for s in strings(v, str(k))]
    if isinstance(data, (list, tuple)):
        return [s for v in data for s in strings(v, key)]
    return []


def unverified(texts: list[str], source: str) -> list[str]:
    """Цифры и «статистика» из текстов, которых нет в брифе: как написано в тексте, без повторов."""
    known = known_numbers(source)
    low = (source or "").lower()
    out: list[str] = []

    def add(found: str) -> None:
        found = re.sub(r"\s+", " ", found).strip(" .,;:")
        if found and found.lower() not in (x.lower() for x in out):
            out.append(found)

    for text in texts:
        text = plain(text)
        for m in CLAIM.finditer(text):
            a, b, unit = _norm(m.group(1)), m.group(2), m.group(4)
            if unit.lower().startswith("год") and not b and a.isdigit() and 1900 <= int(a) <= 2100:
                continue  # «в 2025 году» — дата, а не обещание
            nums = [a] + ([_norm(b)] if b else [])
            if all(n in known or _scale(n, unit) in known for n in nums):
                continue
            add(m.group(0))
        for rx in (RATIO, WORDS):
            for m in rx.finditer(text):
                if re.sub(r"\s+", " ", m.group(0).lower()) not in low:
                    add(m.group(0))
    return out[:12]


def says_we(texts: list[str]) -> bool:
    return any(WE.search(plain(t)) for t in texts)


def check(texts: list[str], source: str, who: str = "") -> list[str]:
    """Что владельцу стоит проверить перед публикацией. source — весь бриф текстом, who — поле «Кто вы».
    Брифа нет — сверять не с чем, молчим."""
    if not (source or "").strip():
        return []
    notes = unverified(texts, source)
    if solo(who) and says_we(texts):
        notes.append(WE_NOTE)
    return notes


def check_line(notes: list[str]) -> str:
    """Строка для карточки материала."""
    nums = [n for n in notes if n != WE_NOTE]
    parts = []
    if nums:
        parts.append("этого нет в брифе: " + ", ".join(nums))
    if WE_NOTE in notes:
        parts.append(WE_NOTE)
    return "Проверьте перед публикацией — " + "; ".join(parts) if parts else ""


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_URL = re.compile(r"https?://\S+|\bwww\.\S+", re.I)


def fit_threads(text: str, limit: int = THREADS_LIMIT) -> str:
    """Текст для Threads: без хэштегов, не длиннее лимита площадки. Режем по концу предложения, а не посреди слова."""
    text = plain(text)
    text = re.sub(r"(?<!\w)#[\w-]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    out = ""
    for para in text.split("\n"):
        for sentence in _SENTENCE_END.split(para):
            piece = (" " if out and not out.endswith("\n") else "") + sentence
            if len(out) + len(piece) > limit:
                out = out.rstrip()
                if out:
                    return out
                cut = text[:limit - 1]
                return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,.;:—-") + "…"
            out += piece
        out += "\n"
    return out.strip()
