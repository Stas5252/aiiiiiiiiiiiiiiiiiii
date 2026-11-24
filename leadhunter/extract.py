import re

PHONE_RE = re.compile(r"(?:\+7|8|\+375|\+380|\+7\s?7)[\s\-(]*\d{2,3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
USERNAME_RE = re.compile(r"(?<![\w/.])@([a-zA-Z][\w]{4,31})\b")
# t.me/username — личка или канал автора. Не берём приглашения (t.me/+…), посты (t.me/chan/123) и служебные пути.
TME_RE = re.compile(r"(?<![\w.])(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z][\w]{4,31})(?![\w/])", re.I)
TME_SERVICE = {"joinchat", "addstickers", "addlist", "share", "proxy", "socks", "iv", "login", "setlanguage"}

_NUM = r"\d{1,3}(?:[ \u00a0]\d{3})+|\d+(?:[.,]\d+)?"
_UNIT = r"млн\.?|миллион\w*|тыс\.?|т\.\s?р\.?|к|k|₽|руб\.?|рублей|р\.|\$|usd|€"
BUDGET_RE = re.compile(
    rf"(?<![\w.,])(?:(?P<lo>{_NUM})\s?[-–—]\s?)?(?P<hi>{_NUM})(?P<gap>\s?)(?P<unit>{_UNIT})(?!\w)(?P<dot>\.?)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?:19|20)\d\d")
_YEAR_PREP_RE = re.compile(r"(?:^|\s)(?:в|с|к|до|после|от)\s+$", re.I)
URL_RE = re.compile(r"https?://\S+")


def extract_contacts(text: str) -> str:
    """Телефон / почта / @username / t.me/username из текста — чтобы писать клиенту сразу, не открывая источник."""
    found: list[str] = []
    for m in PHONE_RE.findall(text):
        found.append(re.sub(r"\s+", " ", m.strip()))
    found += EMAIL_RE.findall(text)
    found += [f"@{u}" for u in USERNAME_RE.findall(text)]
    found += [f"@{u}" for u in TME_RE.findall(text) if u.lower() not in TME_SERVICE]
    return ", ".join(dict.fromkeys(found))


def _is_budget(m: re.Match, text: str) -> bool:
    unit = m["unit"].lower()
    if unit in ("к", "k") and m["gap"] and m["dot"]:
        return False  # «3 к. дома» — корпус, а не тысячи; «50к.» в конце фразы — сумма
    if unit == "р." and not m["lo"] and _YEAR_RE.fullmatch(m["hi"]) and _YEAR_PREP_RE.search(text[:m.start()]):
        return False  # «в 2024 р.» — год
    return True


def extract_budget(text: str) -> str:
    """Первая сумма из текста: «50 000 руб», «30 к», диапазон «5–7 тыс», «1.5 млн»."""
    for m in BUDGET_RE.finditer(text):
        if _is_budget(m, text):
            amount = f"{m['lo']}–{m['hi']}" if m["lo"] else m["hi"]
            return f"{amount} {m['unit']}"
    return ""


_MULT = {"млн": 1_000_000, "миллион": 1_000_000, "тыс": 1000, "т": 1000, "к": 1000, "k": 1000}
_RUB_UNIT_RE = re.compile(r"₽|руб|р\.|т\.\s?р", re.I)  # единица сама называет рубли
_RUB_AFTER_RE = re.compile(r"\.?\s*(?:₽|руб|р\b)", re.I)  # «30 тыс. руб»
_CUE_RE = re.compile(r"(?:бюджет|оплат|цен[аыу]|стоимост|плачу|гонорар|вознагражд)\w*[\s:—–-]*(?:\S+\s+){0,2}$", re.I)
_PER_UNIT_RE = re.compile(r"\s*(?:/|в\s|за\s)", re.I)
_FROM_RE = re.compile(r"(?:^|\W)от\s+$", re.I)  # «от 5000 руб» — нижняя граница, а не весь бюджет


def budget_rub(text: str) -> int | None:
    """Бюджет заказа в рублях — самая большая сумма, названная в тексте. None — если её нельзя назвать
    уверенно: суммы нет, она в валюте или «от …». Не считаются ставки «за час / в месяц / за пост» и
    числа без рублей и без слова про деньги рядом («канал на 3 тыс. подписчиков», «30к просмотров»)."""
    best = None
    for m in BUDGET_RE.finditer(text):
        if not _is_budget(m, text):
            continue
        unit, head, tail = m["unit"].lower(), text[:m.start()], text[m.end():]
        if unit in ("$", "usd", "€"):
            return None
        if not _RUB_UNIT_RE.match(unit):
            rub = _RUB_AFTER_RE.match(tail)
            if rub:
                tail = tail[rub.end():]
            elif not _CUE_RE.search(head[-40:]):
                continue
        if _PER_UNIT_RE.match(tail):
            continue
        if _FROM_RE.search(head):
            return None
        try:
            amount = float(re.sub(r"[ \u00a0]", "", m["hi"]).replace(",", "."))
        except ValueError:
            continue
        best = max(best or 0, int(amount * next((v for k, v in _MULT.items() if unit.startswith(k)), 1)))
    return best


def strip_urls(text: str) -> str:
    return URL_RE.sub("", text)
