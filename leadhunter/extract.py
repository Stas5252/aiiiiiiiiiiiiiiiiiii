import re

PHONE_RE = re.compile(r"(?:\+7|8|\+375|\+380|\+7\s?7)[\s\-(]*\d{2,3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
USERNAME_RE = re.compile(r"(?<![\w/.])@([a-zA-Z][\w]{4,31})\b")
BUDGET_RE = re.compile(
    r"(?:бюджет[:\s]*)?(\d{1,3}(?:[ \u00a0]?\d{3})*(?:[.,]\d+)?)\s?(к|k|тыс\.?|т\.р\.?|₽|руб\.?|р\.|рублей|\$|usd|€)(?!\w)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://\S+")


def extract_contacts(text: str) -> str:
    """Телефон / почта / @username из текста — чтобы писать клиенту сразу, не открывая источник."""
    found: list[str] = []
    for m in PHONE_RE.findall(text):
        found.append(re.sub(r"\s+", " ", m.strip()))
    found += EMAIL_RE.findall(text)
    found += [f"@{u}" for u in USERNAME_RE.findall(text)]
    return ", ".join(dict.fromkeys(found))


def extract_budget(text: str) -> str:
    m = BUDGET_RE.search(text)
    return f"{m.group(1)} {m.group(2)}".strip() if m else ""


def strip_urls(text: str) -> str:
    return URL_RE.sub("", text)
