"""Разовый вход на площадку в своём браузере: python -m leadhunter.browser.login instagram

Открывает окно, вы логинитесь руками, нажимаете Enter в консоли — профиль с куками сохранён в data/profiles/<site>.
Папку профиля можно скопировать на сервер."""
import asyncio
import sys

from ..config import Settings
from .session import BrowserSession

LOGIN_URLS = {
    "instagram": "https://www.instagram.com/accounts/login/",
    "threads": "https://www.threads.net/login",
    "youdo": "https://youdo.com/",
    "profi": "https://profi.ru/backoffice/",
}


async def main(site: str) -> None:
    s = Settings()
    async with BrowserSession(s.browser_profiles_dir, site, s.proxy_url, headless=False) as b:
        await b.open(LOGIN_URLS.get(site, f"https://{site}"))
        print(f"Залогиньтесь в окне браузера ({site}), затем нажмите Enter здесь…")
        await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
    print(f"Готово: профиль сохранён в {s.browser_profiles_dir}/{site}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python -m leadhunter.browser.login instagram|threads|youdo|profi")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
