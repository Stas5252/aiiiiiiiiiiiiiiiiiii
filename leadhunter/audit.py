import logging
import re

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)
VIEWPORT_RE = re.compile(r'<meta[^>]+name=["\']viewport["\']', re.I)
PARKED_RE = re.compile(r"(домен (продаётся|продается|припаркован)|this domain is for sale|срок регистрации домена|домен не прилинкован|default web page|site is under construction|сайт в разработке)", re.I)


async def audit_site(url: str, proxy: str | None = None, timeout: int = 15) -> str | None:
    """Проверяет сайт из карточки организации. Возвращает описание проблемы — повод предложить переделку."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        async with AsyncSession(impersonate="chrome", proxy=proxy, timeout=timeout) as s:
            r = await s.get(url, allow_redirects=True)
    except Exception as e:
        return f"сайт не открывается ({type(e).__name__})"
    if r.status_code >= 400:
        return f"сайт отдаёт ошибку {r.status_code}"
    body = r.text[:300_000]
    problems = []
    if str(r.url).startswith("http://"):
        problems.append("без HTTPS")
    if not VIEWPORT_RE.search(body):
        problems.append("не адаптирован под телефон")
    if PARKED_RE.search(body):
        problems.append("заглушка вместо сайта")
    if len(body) < 1500:
        problems.append("почти пустая страница")
    return ", ".join(problems) if problems else None
