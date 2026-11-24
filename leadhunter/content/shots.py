"""Снимок сайта для слайда с кейсом: верх страницы в окне браузера. Открываются только ссылки из брифа владельца
и только адреса из интернета; снимок хранится неделю, чтобы не ходить на сайт при каждой пересборке."""
import asyncio
import hashlib
import logging
import os
import re
import time
from urllib.parse import urlsplit

from .. import brief

log = logging.getLogger(__name__)
TTL = 7 * 86400
TIMEOUT = 45
VIEW = (1280, 1600)  # высокое окно: плашка про cookie прижата к низу и в снимок верха не попадает
CLIP = (1280, 800)
URL = re.compile(r"https?://[^\s<>\"'«»]+", re.I)
MAX_SHOTS = 2


def norm(url: str) -> str:
    return str(url or "").strip().rstrip("/.,;:)")


def brief_urls(data: dict) -> set[str]:
    """Ссылки, которые владелец сам указал в брифе (кейсы, контакты)."""
    return {norm(u) for value in data.values() for u in URL.findall(str(value or ""))}


async def _is_public(host: str, port: int, seen: dict) -> bool:
    if host not in seen:
        try:
            await brief._public_ip(host, port)
            seen[host] = True
        except brief.BriefError:
            seen[host] = False
    return seen[host]


async def _capture(url: str) -> bytes:
    from patchright.async_api import async_playwright

    seen: dict[str, bool] = {}

    async def guard(route):
        parts = urlsplit(route.request.url)
        ok = parts.scheme in ("http", "https") and parts.hostname and await _is_public(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), seen)
        await (route.continue_() if ok else route.abort())

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception:
            browser = await pw.chromium.launch(headless=True, channel="chrome")
        try:
            ctx = await browser.new_context(viewport={"width": VIEW[0], "height": VIEW[1]}, locale="ru-RU",
                                            service_workers="block", accept_downloads=False)
            await ctx.route("**/*", guard)
            page = await ctx.new_page()
            await page.goto(url, wait_until="load", timeout=25000)
            await page.wait_for_timeout(1500)
            return await page.screenshot(type="jpeg", quality=88, clip={"x": 0, "y": 0, "width": CLIP[0], "height": CLIP[1]})
        finally:
            await browser.close()


async def site_shot(url: str, cache_dir: str, *, ttl: float = TTL) -> str | None:
    """Путь к снимку сайта или None, если снять не удалось (слайд тогда рисуется без снимка)."""
    url = norm(url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, hashlib.sha1(url.encode()).hexdigest()[:20] + ".jpg")
    try:
        if os.path.getsize(path) > 0 and time.time() - os.path.getmtime(path) < ttl:
            return path
    except OSError:
        pass
    try:
        data = await asyncio.wait_for(_capture(url), TIMEOUT)
    except Exception as e:
        log.warning("снимок сайта %s не получился: %s", parts.hostname, str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
        return path if os.path.isfile(path) and os.path.getsize(path) > 0 else None  # старый снимок лучше, чем никакого
    with open(path, "wb") as f:
        f.write(data)
    return path
