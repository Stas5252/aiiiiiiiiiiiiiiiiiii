"""Снимки HTML-страниц браузером: слайды, обложки и кадры анимации.
Страницы свои, скрипты на них выключены, сеть закрыта — открываются только файлы из разрешённых папок."""
import asyncio
import logging
import os
import tempfile
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .design import FONTS_DIR

log = logging.getLogger(__name__)
TIMEOUT = 240  # секунд на один вызов: дольше — рисуем запасным способом
FPS = 25
# ужимаем шрифт, пока текст не влезет в слайд; совсем длинное слово — переносим по буквам
FIT_JS = """() => {
  const mid = document.querySelector('.mid'), box = document.querySelector('.in');
  if (!mid || !box) return 1;
  const over = () => box.offsetHeight > mid.clientHeight - 48 ||
    [...box.querySelectorAll('h1,h2,p,.pill')].some(e => e.scrollWidth > e.clientWidth + 1);
  let k = 1;
  while (over() && k > 0.46) { k -= 0.04; document.body.style.setProperty('--k', k.toFixed(2)); }
  if (over()) document.body.classList.add('tight');
  return k;
}"""
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


class RenderUnavailable(RuntimeError):
    """Браузера нет или он не запустился — вызывающий рисует запасным способом."""


def _lock() -> asyncio.Lock:
    return _locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())  # два браузера сразу — лишняя память


async def _launch(pw):
    try:
        return await pw.chromium.launch(headless=True)
    except Exception as first:
        try:
            return await pw.chromium.launch(headless=True, channel="chrome")  # установленный в системе Chrome
        except Exception:
            raise RenderUnavailable(f"браузер не запустился: {str(first).splitlines()[0][:200]}") from first


def _inside(path: str, roots: list[str]) -> bool:
    path = os.path.realpath(path)
    return any(path == root or path.startswith(root + os.sep) for root in roots)


@asynccontextmanager
async def _browser(size, allow: tuple[str, ...]):
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        raise RenderUnavailable("не установлен patchright") from e
    async with _lock():
        with tempfile.TemporaryDirectory(prefix="lhslides_") as tmp:
            roots = [os.path.realpath(p) for p in (tmp, FONTS_DIR, *allow)]

            async def guard(route):
                url = urlsplit(route.request.url)
                if url.scheme == "file" and _inside(unquote(url.path), roots):
                    await route.continue_()
                else:
                    await route.abort()

            async with async_playwright() as pw:
                browser = await _launch(pw)
                try:
                    ctx = await browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1,
                                                    java_script_enabled=False, locale="ru-RU")
                    await ctx.route("**/*", guard)
                    page = await ctx.new_page()
                    count = 0

                    async def load(html: str):
                        nonlocal count
                        count += 1
                        path = os.path.join(tmp, f"p{count}.html")
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(html)
                        await page.goto(Path(path).as_uri(), wait_until="load")
                        await page.evaluate("() => document.fonts.ready.then(() => 1)")
                        await page.evaluate(FIT_JS)
                        return page

                    yield load
                finally:
                    await browser.close()


async def _guarded(coro):
    try:
        return await asyncio.wait_for(coro, TIMEOUT)
    except RenderUnavailable:
        raise
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise RenderUnavailable("браузер не успел нарисовать страницы") from e
    except Exception as e:
        raise RenderUnavailable(f"браузер не нарисовал страницы: {type(e).__name__}: {str(e).splitlines()[0][:200]}") from e


async def render_pages(pages: list[str], size, *, allow: tuple[str, ...] = ()) -> list[bytes]:
    """PNG каждой страницы. allow — папки, из которых страницам можно брать картинки."""
    async def run():
        async with _browser(size, allow) as load:
            return [await (await load(html)).screenshot(type="png") for html in pages]
    return await _guarded(run()) if pages else []


async def render_motion(pages: list[str], size, seconds: list[float], *, allow: tuple[str, ...] = ()) -> list[list[bytes]]:
    """Кадры анимации каждой страницы (JPEG, 25 в секунду): столько секунд, сколько указано для страницы."""
    async def run():
        out = []
        async with _browser(size, allow) as load:
            for html, sec in zip(pages, seconds):
                page = await load(html)
                await page.evaluate("() => document.getAnimations().forEach(a => a.pause())")
                frames = []
                for i in range(max(1, round(sec * FPS))):
                    await page.evaluate("t => document.getAnimations().forEach(a => { a.currentTime = t })", i * 1000 / FPS)
                    frames.append(await page.screenshot(type="jpeg", quality=92))
                out.append(frames)
        return out
    return await _guarded(run()) if pages else []
