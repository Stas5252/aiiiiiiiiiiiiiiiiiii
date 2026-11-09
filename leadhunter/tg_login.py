"""Разовая авторизация Telegram-аккаунта: python -m leadhunter.tg_login"""
import asyncio

from telethon import TelegramClient

from .config import Settings


async def login() -> None:
    s = Settings()
    assert s.tg_api_id and s.tg_api_hash, "Заполни TG_API_ID и TG_API_HASH в .env (my.telegram.org)"
    async with TelegramClient(s.tg_session, s.tg_api_id, s.tg_api_hash) as client:
        me = await client.get_me()
        print(f"Готово: сессия сохранена в {s.tg_session}.session, аккаунт @{me.username or me.id}")


if __name__ == "__main__":
    asyncio.run(login())
