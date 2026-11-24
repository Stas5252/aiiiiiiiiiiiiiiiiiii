import logging
import re

log = logging.getLogger(__name__)
VIEWPORT_RE = re.compile(r'<meta[^>]+name=["\']viewport["\']', re.I)
PARKED_RE = re.compile(r"(домен (продаётся|продается|припаркован)|this domain is for sale|срок регистрации домена|домен не прилинкован|default web page|site is under construction|сайт в разработке)", re.I)


async def audit_site(url: str, timeout: int = 15) -> str | None:
    """Проверяет сайт из карточки организации. Возвращает описание проблемы — повод предложить переделку.
    Ссылку в карточку вписывает сама компания, поэтому открываем её так же, как ссылку из текста заказа:
    только адреса из интернета, каждая переадресация проверяется (analysis.fetch). Прокси не используется."""
    from .analysis.fetch import fetch  # импорт здесь: analysis сам подключает источники карт

    r = await fetch(url, timeout=timeout)
    if r.error:
        return r.error if r.error.startswith("сайт") else f"сайт не открывается ({r.error})"
    if r.status >= 400:
        return f"сайт отдаёт ошибку {r.status}"
    body = r.body[:300_000]
    problems = []
    if r.url.startswith("http://"):
        problems.append("без HTTPS")
    if not VIEWPORT_RE.search(body):
        problems.append("не адаптирован под телефон")
    if PARKED_RE.search(body):
        problems.append("заглушка вместо сайта")
    if len(body) < 1500:
        problems.append("почти пустая страница")
    return ", ".join(problems) if problems else None
