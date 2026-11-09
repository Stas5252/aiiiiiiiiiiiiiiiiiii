import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..db import Database
from ..models import Lead
from ..pipeline import Enriched

log = logging.getLogger(__name__)

ICONS = {"telegram": "✈️", "threads": "🧵", "instagram": "📸", "yandex_maps": "🗺", "hh.ru": "💼", "vk": "🔵",
         "rss": "📋", "web": "🌐"}
URGENCY = {"высокая": "🔴", "средняя": "🟡", "низкая": "🟢"}


def status_keyboard(lead_id: int, has_draft: bool = False, chat_key: str | None = None) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ Написал", callback_data=f"st:{lead_id}:contacted"),
        InlineKeyboardButton(text="🔥 Горячий", callback_data=f"st:{lead_id}:hot"),
        InlineKeyboardButton(text="🚫 Не лид", callback_data=f"st:{lead_id}:spam"),
    ]]
    extra = []
    if has_draft:
        extra.append(InlineKeyboardButton(text="✍️ Черновик ответа", callback_data=f"dr:{lead_id}"))
    if chat_key:
        extra.append(InlineKeyboardButton(text="🔇 Чат в игнор", callback_data=f"mute:{lead_id}"))
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_lead(lead_id: int, lead: Lead, info: Enriched) -> str:
    icon = ICONS.get(lead.source.split(":")[0], "📌")
    e = html.escape
    head = f"{icon} <b>#{lead_id} · {e(lead.source)} · {info.score}/10</b>"
    if info.service:
        head += f" · 🎯 {e(info.service)}"
    if info.urgency:
        head += f" {URGENCY.get(info.urgency, '')}"
    lines = [head]
    if lead.title:
        lines.append(f"<b>{e(lead.title[:200])}</b>")
    if info.summary:
        lines.append(f"🤖 {e(info.summary)}")
    lines.append(e(lead.text[:900]) + ("…" if len(lead.text) > 900 else ""))
    meta = []
    if lead.author:
        meta.append(f"👤 {e(lead.author)}")
    if lead.contact:
        meta.append(f"📞 {e(lead.contact)}")
    if info.budget:
        meta.append(f"💰 {e(info.budget)}")
    if meta:
        lines.append(" · ".join(meta))
    tags = info.res.topic + info.res.intent
    if tags:
        lines.append("🏷 " + ", ".join(e(t) for t in tags))
    if lead.url:
        lines.append(f'<a href="{e(lead.url)}">Открыть источник</a>')
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, bot: Bot, db: Database, owner_ids: list[int]):
        self.bot = bot
        self.db = db
        self.static_owners = owner_ids

    async def owners(self) -> list[int]:
        stored = await self.db.get_setting("owner_ids", "")
        return self.static_owners or [int(x) for x in stored.split(",") if x]

    async def send(self, text: str, **kwargs) -> None:
        for chat_id in await self.owners():
            for _ in range(2):
                try:
                    await self.bot.send_message(chat_id, text, disable_web_page_preview=True, **kwargs)
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception:
                    log.exception("не удалось отправить сообщение в %s", chat_id)
                    break

    async def notify(self, lead_id: int, lead: Lead, info: Enriched) -> None:
        kb = status_keyboard(lead_id, has_draft=bool(info.draft), chat_key=lead.extra.get("chat_key"))
        await self.send(format_lead(lead_id, lead, info), reply_markup=kb)
