import logging
import os
import random

log = logging.getLogger(__name__)

# Живые размеры экранов — одинаковый viewport у всех ботов палится первым
VIEWPORTS = [(1366, 768), (1536, 864), (1440, 900), (1920, 1080), (1680, 1050)]


class BrowserSession:
    """Антидетект-браузер (Patchright = Playwright без следов автоматизации) с постоянным профилем на площадку.
    Логин делается один раз руками через `python -m leadhunter.browser.login <site>` — куки живут в профиле."""

    def __init__(self, profiles_dir: str, site: str, proxy: str | None = None, headless: bool = True):
        self.profile_dir = os.path.join(profiles_dir, site)
        self.proxy = proxy
        self.headless = headless
        self._pw = None
        self.ctx = None

    async def __aenter__(self):
        from patchright.async_api import async_playwright

        os.makedirs(self.profile_dir, exist_ok=True)
        self._pw = await async_playwright().start()
        w, h = random.choice(VIEWPORTS)
        kwargs = dict(
            user_data_dir=self.profile_dir,
            headless=self.headless,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": w, "height": h},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self.proxy:
            kwargs["proxy"] = {"server": self.proxy}
        self.ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        return self

    async def __aexit__(self, *exc):
        if self.ctx:
            await self.ctx.close()
        if self._pw:
            await self._pw.stop()

    async def api_json(self, url: str, headers: dict | None = None) -> dict | None:
        """GET внутреннего JSON-эндпоинта с куками профиля. None — если отдали HTML (разлогинило)."""
        r = await self.ctx.request.get(url, headers=headers or {})
        ctype = r.headers.get("content-type", "")
        if r.status != 200 or "json" not in ctype:
            log.warning("browser api %s -> %s %s", url.split("?")[0], r.status, ctype)
            return None
        return await r.json()

    async def open(self, url: str):
        page = await self.ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        return page
