"""Загрузка чужих страниц для досье: только адреса из интернета, с ограничением размера и переадресаций."""
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .. import brief

MAX_BYTES = 600_000
REDIRECTS = 4


@dataclass
class Page:
    url: str  # адрес после переадресаций
    status: int = 0
    body: str = ""
    seconds: float = 0.0
    error: str = ""  # почему страницы нет — простыми словами

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 400


def normalize_url(url: str) -> str:
    url = url.strip()
    return url if url.lower().startswith(("http://", "https://")) else "https://" + url


def puny(host: str) -> str:
    """«ваш-мастеръ.рф» → xn--…: так домен записан в DNS и в адресах из поисковой выдачи."""
    try:
        return host.encode("idna").decode().lower()
    except UnicodeError:
        return host.lower()


async def fetch(url: str, *, timeout: int = 15, max_bytes: int = MAX_BYTES) -> Page:
    """Страница целиком не нужна — читаем первые max_bytes. Ошибки не бросаем: «сайт не открывается» — тоже факт для досье.
    Адрес (и каждый адрес переадресации) закрепляется за проверенным IP из интернета — как в brief.site_text:
    ссылка из текста заказа не должна открывать службы на машине с ботом."""
    from curl_cffi import CurlOpt
    from curl_cffi.requests import AsyncSession

    url = normalize_url(url)
    started = time.monotonic()
    for _ in range(REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return Page(url, error="это не ссылка на сайт")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        host = puny(parts.hostname)
        if host != parts.hostname:
            url = parts._replace(netloc=host + (f":{parts.port}" if parts.port else "")).geturl()
        try:
            ip = await brief._public_ip(host, port)
        except brief.BriefError as e:
            return Page(url, error="сайт не найден" if "не найден" in str(e) else "адрес не из интернета")
        pin = f"{host}:{port}:{'[' + ip + ']' if ':' in ip else ip}"
        try:
            async with AsyncSession(impersonate="chrome", timeout=timeout, curl_options={CurlOpt.RESOLVE: [pin]}) as s:
                r = await s.get(url, allow_redirects=False, stream=True)
                try:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    body = b""
                    async for chunk in r.aiter_content():
                        body += chunk
                        if len(body) >= max_bytes:
                            break
                finally:
                    await r.aclose()
        except Exception as e:
            return Page(url, error=f"сайт не открывается ({type(e).__name__})")
        return Page(url, r.status_code, body[:max_bytes].decode(r.encoding or "utf-8", "replace"),
                    round(time.monotonic() - started, 2))
    return Page(url, error="слишком много переадресаций")
