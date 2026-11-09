import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User

from ..config import Settings
from ..models import Lead
from .base import Emit, Source

log = logging.getLogger(__name__)


def message_url(chat, msg_id: int) -> str:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if isinstance(chat, Channel):
        return f"https://t.me/c/{chat.id}/{msg_id}"
    return ""


def sender_info(sender) -> tuple[str, str]:
    if isinstance(sender, User):
        name = " ".join(filter(None, [sender.first_name, sender.last_name])) or str(sender.id)
        contact = f"@{sender.username}" if sender.username else f"tg://user?id={sender.id}"
        return name, contact
    if sender is not None:
        return getattr(sender, "title", "") or str(sender.id), ""
    return "", ""


class TelegramChatsSource(Source):
    """Читает группы через обычный аккаунт (Telethon): realtime + периодический глобальный поиск."""

    name = "telegram"

    def __init__(self, s: Settings):
        self.client = TelegramClient(s.tg_session, s.tg_api_id, s.tg_api_hash)
        self.chats = s.tg_chats
        self.search_words = s.tg_search_words
        self.interval = s.tg_search_interval

    def _to_lead(self, message, chat, sender) -> Lead:
        author, contact = sender_info(sender)
        chat_title = getattr(chat, "title", "") or getattr(chat, "username", "") or str(getattr(chat, "id", ""))
        return Lead(
            source="telegram",
            external_id=f"{getattr(chat, 'id', 0)}:{message.id}",
            text=message.raw_text or "",
            title=f"Чат: {chat_title}",
            url=message_url(chat, message.id),
            author=author,
            contact=contact,
            found_at=message.date or datetime.now(timezone.utc),
        )

    async def run(self, emit: Emit) -> None:
        await self.client.start()
        me = await self.client.get_me()
        log.info("telegram: вошли как %s", me.username or me.id)

        chats = self.chats or None

        @self.client.on(events.NewMessage(chats=chats, incoming=True))
        async def on_message(event):
            if not (event.is_group or event.is_channel) or not event.raw_text:
                return
            chat = await event.get_chat()
            sender = await event.get_sender()
            await emit(self._to_lead(event.message, chat, sender))

        asyncio.create_task(self._search_loop(emit))
        await self.client.run_until_disconnected()

    async def _search_loop(self, emit: Emit) -> None:
        # Подстраховка от пропусков realtime: глобальный поиск по словам во всех диалогах аккаунта
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        while True:
            started = datetime.now(timezone.utc)
            try:
                for word in self.search_words:
                    async for m in self.client.iter_messages(None, search=word, limit=50):
                        if m.date and m.date < since:
                            break
                        chat = await m.get_chat()
                        if isinstance(chat, User) or not (m.raw_text or ""):
                            continue
                        if self.chats and not isinstance(chat, (Channel, Chat)):
                            continue
                        await emit(self._to_lead(m, chat, await m.get_sender()))
                    await asyncio.sleep(2)
            except Exception:
                log.exception("telegram: ошибка глобального поиска")
            since = started - timedelta(minutes=5)
            await asyncio.sleep(self.interval)
