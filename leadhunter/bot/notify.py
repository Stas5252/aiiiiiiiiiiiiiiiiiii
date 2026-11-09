import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..db import Database
from ..models import Lead
from ..pipeline import Enriched
from .workspace import Workspace, route

log = logging.getLogger(__name__)

ICONS = {"telegram": "✈️", "threads": "🧵", "instagram": "📸", "yandex_maps": "🗺", "site_audit": "🩺", "hh.ru": "💼",
         "vk": "🔵", "rss": "📋", "web": "🌐", "kwork": "🟢", "weblancer": "🟣", "freelance.ru": "🟠"}
URGENCY = {"высокая": "🔴", "средняя": "🟡", "низкая": "🟢"}
STATUS_LABELS = {"new": "новый", "taken": "в работе", "contacted": "написали", "replied": "ответил",
                 "deal": "🤝 сделка", "lost": "отказ", "spam": "не лид", "hot": "🔥 горячий"}


def _extra_row(lead_id: int, has_draft: bool, can_send: bool, chat_key: str | None) -> list[InlineKeyboardButton]:
    row = []
    if has_draft:
        row.append(InlineKeyboardButton(text="✍️ Черновик", callback_data=f"dr:{lead_id}"))
    if can_send:
        row.append(InlineKeyboardButton(text="📨 Отправить с аккаунта", callback_data=f"send:{lead_id}"))
    if chat_key:
        row.append(InlineKeyboardButton(text="🔇 Чат в игнор", callback_data=f"mute:{lead_id}"))
    return row


def new_lead_keyboard(lead_id: int, has_draft: bool = False, chat_key: str | None = None,
                      can_send: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="👋 Беру", callback_data=f"take:{lead_id}"),
        InlineKeyboardButton(text="🔥 Горячий", callback_data=f"st:{lead_id}:hot"),
        InlineKeyboardButton(text="🚫 Не лид", callback_data=f"st:{lead_id}:spam"),
    ]]
    extra = _extra_row(lead_id, has_draft, can_send, chat_key)
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def taken_keyboard(lead_id: int, has_draft: bool = False, can_send: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ Написал", callback_data=f"st:{lead_id}:contacted"),
        InlineKeyboardButton(text="💬 Ответил", callback_data=f"st:{lead_id}:replied"),
        InlineKeyboardButton(text="🤝 Сделка", callback_data=f"st:{lead_id}:deal"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"st:{lead_id}:lost"),
    ]]
    extra = _extra_row(lead_id, has_draft, can_send, None)
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def followup_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Ответил", callback_data=f"st:{lead_id}:replied"),
        InlineKeyboardButton(text="⏰ Напомнить через 3 дня", callback_data=f"fu:{lead_id}:72"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"st:{lead_id}:lost"),
    ]])


# Совместимость со старым именем
status_keyboard = new_lead_keyboard


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
    if info.author_bio:
        lines.append(f"ℹ️ {e(info.author_bio[:200])}")
    tags = info.res.topic + info.res.intent
    if tags:
        lines.append("🏷 " + ", ".join(e(t) for t in tags))
    if info.freshness:
        lines.append(f"⏱ {e(info.freshness)}")
    if info.rule:
        lines.append(f"🎯 правило: <code>{e(info.rule)}</code>")
    if lead.url:
        lines.append(f'<a href="{e(lead.url)}">Открыть источник</a>')
    return "\n".join(lines)


class TelegramNotifier:
    """Доставка: в форум-группу по темам (если настроена) и/или в личку владельцам."""

    def __init__(self, bot: Bot, db: Database, owner_ids: list[int], *, outreach_enabled: bool = False,
                 hot_score: int = 8, deliver_dm: bool = False):
        self.bot = bot
        self.db = db
        self.static_owners = owner_ids
        self.outreach_enabled = outreach_enabled
        self.hot_score = hot_score
        self.deliver_dm = deliver_dm
        self.workspace = Workspace(bot, db)

    async def owners(self) -> list[int]:
        stored = await self.db.get_setting("owner_ids", "")
        return self.static_owners or [int(x) for x in stored.split(",") if x]

    async def _send_dm(self, chat_id: int, text: str, **kwargs):
        for _ in range(2):
            try:
                return await self.bot.send_message(chat_id, text, disable_web_page_preview=True, **kwargs)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                log.exception("не удалось отправить сообщение в %s", chat_id)
                return None

    async def send(self, text: str, *, topic: str = "health", silent: bool = True, **kwargs) -> None:
        """Служебное сообщение (алерт, дайджест): в тему группы, иначе владельцам."""
        if await self.workspace.chat_id():
            try:
                await self.workspace.send(topic, text, silent=silent, **kwargs)
                return
            except Exception:
                log.exception("не удалось отправить в группу")
        for chat_id in await self.owners():
            await self._send_dm(chat_id, text, **kwargs)

    async def notify_row(self, row) -> None:
        """Повторная доставка лида из базы (например, накопленного до появления владельца)."""
        import json
        from datetime import datetime

        from ..scoring import ScoreResult

        matched = json.loads(row["matched"] or "{}")
        res = ScoreResult(row["score"], matched.get("topic", []), matched.get("intent", []), matched.get("negative", []))
        lead = Lead(source=row["source"], external_id=row["external_id"], text=row["text"] or "", url=row["url"] or "",
                    title=row["title"] or "", author=row["author"] or "", contact=row["contact"] or "",
                    found_at=datetime.fromisoformat(row["found_at"]),
                    extra={"chat_key": row["chat_key"], "chat_title": row["chat_title"]})
        info = Enriched(row["score"], res, summary=row["summary"], service=row["service"] or "", budget=row["budget"] or "",
                        urgency=row["urgency"] or "", draft=row["draft"] or "", rule=row["rule"] or "",
                        author_bio=row["author_bio"] or "", freshness=(row["freshness"] or "") if "freshness" in row.keys() else "")
        await self.notify(row["id"], lead, info)

    async def notify(self, lead_id: int, lead: Lead, info: Enriched) -> None:
        can_send = (self.outreach_enabled and lead.source == "telegram" and bool(info.draft)
                    and lead.contact.startswith(("@", "tg://")))
        kb = new_lead_keyboard(lead_id, has_draft=bool(info.draft), chat_key=lead.extra.get("chat_key"), can_send=can_send)
        text = format_lead(lead_id, lead, info)
        delivered = False
        if await self.workspace.chat_id():
            key = route(lead, info, self.hot_score)
            try:
                msg = await self.workspace.send(key, text, silent=(key != "hot"), reply_markup=kb)
                if msg:
                    await self.db.set_lead_message(lead_id, msg.chat.id, msg.message_id, msg.message_thread_id)
                    delivered = True
            except Exception:
                log.exception("не удалось отправить лид %s в группу", lead_id)
        if not delivered or self.deliver_dm:
            for chat_id in await self.owners():
                msg = await self._send_dm(chat_id, text, reply_markup=kb)
                if msg and not delivered:
                    await self.db.set_lead_message(lead_id, msg.chat.id, msg.message_id, None)
                    delivered = True
