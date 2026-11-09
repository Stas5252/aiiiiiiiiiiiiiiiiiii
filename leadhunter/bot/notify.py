import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..db import Database
from ..models import Lead
from ..scoring import ScoreResult

log = logging.getLogger(__name__)

ICONS = {"telegram": "✈️", "yandex_maps": "🗺", "hh.ru": "💼", "vk": "🔵", "rss": "📋", "web": "🌐"}


def status_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Написал", callback_data=f"st:{lead_id}:contacted"),
        InlineKeyboardButton(text="🔥 Горячий", callback_data=f"st:{lead_id}:hot"),
        InlineKeyboardButton(text="🚫 Не лид", callback_data=f"st:{lead_id}:spam"),
    ]])


def format_lead(lead_id: int, lead: Lead, score: int, res: ScoreResult, summary: str | None) -> str:
    icon = ICONS.get(lead.source.split(":")[0], "📌")
    e = html.escape
    lines = [f"{icon} <b>#{lead_id} · {e(lead.source)} · {score}/10</b>"]
    if lead.title:
        lines.append(f"<b>{e(lead.title[:200])}</b>")
    if summary:
        lines.append(f"🤖 {e(summary)}")
    lines.append(e(lead.text[:900]) + ("…" if len(lead.text) > 900 else ""))
    meta = []
    if lead.author:
        meta.append(f"👤 {e(lead.author)}")
    if lead.contact:
        meta.append(f"📞 {e(lead.contact)}")
    if meta:
        lines.append(" · ".join(meta))
    tags = res.topic + res.intent
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

    async def notify(self, lead_id: int, lead: Lead, score: int, res: ScoreResult, summary: str | None) -> None:
        text = format_lead(lead_id, lead, score, res, summary)
        for chat_id in await self.owners():
            for _ in range(2):
                try:
                    await self.bot.send_message(chat_id, text, reply_markup=status_keyboard(lead_id),
                                                disable_web_page_preview=True)
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception:
                    log.exception("не удалось отправить лид %s в %s", lead_id, chat_id)
                    break
