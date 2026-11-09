"""Проверка доступа: владельцы из .env/первого /start + команда.
В рабочей группе любой её участник становится членом команды при первом нажатии кнопки."""
import logging

from aiogram import Bot
from aiogram.types import User

from ..db import Database
from .workspace import WORKSPACE_KEY

log = logging.getLogger(__name__)


class Guard:
    def __init__(self, bot: Bot, db: Database, static_owners: list[int]):
        self.bot = bot
        self.db = db
        self.static_owners = static_owners

    async def owners(self) -> list[int]:
        stored = await self.db.get_setting("owner_ids", "")
        return self.static_owners or [int(x) for x in stored.split(",") if x]

    async def is_owner(self, user_id: int) -> bool:
        return user_id in await self.owners()

    async def allowed(self, user: User, chat_id: int | None = None) -> bool:
        if await self.is_owner(user.id) or await self.db.is_member(user.id):
            return True
        ws = await self.db.get_setting(WORKSPACE_KEY)
        if ws and chat_id is not None and str(chat_id) == ws:
            try:
                m = await self.bot.get_chat_member(chat_id, user.id)
            except Exception:
                return False
            if m.status in ("member", "administrator", "creator"):
                await self.db.add_member(user.id, user.full_name)
                return True
        return False

    @staticmethod
    def name(user: User) -> str:
        return user.full_name or (f"@{user.username}" if user.username else str(user.id))
