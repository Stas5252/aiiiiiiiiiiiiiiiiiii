"""Кнопки под карточками: взять, статусы, черновик, отправка, игнор чата, follow-up."""
import html
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from .guard import Guard
from .notify import STATUS_LABELS, followup_keyboard, taken_keyboard


def build_callbacks(deps) -> Router:
    router = Router()
    db, guard, tg = deps.db, deps.guard, deps.tg

    async def ok(cq: CallbackQuery) -> bool:
        if await guard.allowed(cq.from_user, cq.message.chat.id if cq.message else None):
            return True
        await cq.answer("Нет доступа", show_alert=False)
        return False

    async def edit(cq: CallbackQuery, text: str | None = None, markup=None) -> None:
        try:
            if text is not None:
                await cq.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            else:
                await cq.message.edit_reply_markup(reply_markup=markup)
        except TelegramBadRequest:
            pass  # сообщение не изменилось или слишком старое — не критично

    def can_send(row) -> bool:
        return (deps.outreach_limit > 0 and row["source"] == "telegram" and bool(row["draft"])
                and str(row["contact"] or "").startswith(("@", "tg://")))

    @router.callback_query(F.data.startswith("take:"))
    async def take(cq: CallbackQuery):
        if not await ok(cq):
            return
        lead_id = int(cq.data.split(":")[1])
        name = Guard.name(cq.from_user)
        row = await db.get_lead(lead_id)
        if not row:
            await cq.answer("Лид не найден")
            return
        if not await db.assign(lead_id, cq.from_user.id, name):
            await cq.answer(f"Уже взял {row['assigned_name']}", show_alert=True)
            return
        text = cq.message.html_text + f"\n\n👤 <b>Взял: {html.escape(name)}</b>"
        await edit(cq, text, taken_keyboard(lead_id, bool(row["draft"]), can_send(row)))
        await cq.answer("Ваш лид. Напишите клиенту и нажмите «Написал»")

    @router.callback_query(F.data.startswith("st:"))
    async def set_status(cq: CallbackQuery):
        if not await ok(cq):
            return
        _, lead_id, status = cq.data.split(":")
        lead_id = int(lead_id)
        name = Guard.name(cq.from_user)
        row = await db.get_lead(lead_id)
        if not row:
            await cq.answer("Лид не найден")
            return
        if status in ("contacted", "replied", "deal", "lost") and not row["assigned_to"]:
            await db.assign(lead_id, cq.from_user.id, name)
        await db.set_status(lead_id, status, cq.from_user.id, name)
        suffix = f"\n\n{STATUS_LABELS.get(status, status)} — {html.escape(name)}"
        if status == "contacted":
            await db.schedule_followup(lead_id, deps.followup_hours)
            await edit(cq, cq.message.html_text + suffix, taken_keyboard(lead_id, bool(row["draft"]), can_send(row)))
            await cq.answer(f"Напомню через {deps.followup_hours} ч, если клиент не ответит")
            return
        if status == "hot":
            await edit(cq, None, cq.message.reply_markup)
            await cq.answer("🔥 Отмечен как горячий — ИИ учтёт как пример")
            return
        await db.clear_followup(lead_id)
        await edit(cq, cq.message.html_text + suffix, None)
        hints = {"spam": "ИИ учтёт как пример «не лид»", "deal": "🎉 Поздравляю!", "replied": "Ведите к сделке",
                 "lost": "Отказ зафиксирован"}
        await cq.answer(hints.get(status, status))

    @router.callback_query(F.data.startswith("fu:"))
    async def followup_later(cq: CallbackQuery):
        if not await ok(cq):
            return
        _, lead_id, hours = cq.data.split(":")
        await db.schedule_followup(int(lead_id), int(hours))
        await edit(cq, None, None)
        await cq.answer(f"Напомню через {int(hours) // 24} дн.")

    @router.callback_query(F.data.startswith("dr:"))
    async def show_draft(cq: CallbackQuery):
        if not await ok(cq):
            return
        row = await db.get_lead(int(cq.data.split(":")[1]))
        draft = row["draft"] if row else None
        if row and not draft and row["service"]:
            draft = await db.get_template(row["service"])
        if not draft:
            await cq.answer("Черновика нет — задайте шаблон: /template_set услуга текст", show_alert=True)
            return
        # Отдельным сообщением моноширинным — копируется одним тапом
        await cq.message.reply(f"✍️ Черновик для #{row['id']}:\n\n<code>{html.escape(draft)}</code>")
        await cq.answer()

    @router.callback_query(F.data.startswith("send:"))
    async def send_draft(cq: CallbackQuery):
        if not await ok(cq):
            return
        row = await db.get_lead(int(cq.data.split(":")[1]))
        if not tg or not row or not row["draft"] or not row["contact"]:
            await cq.answer("Нечего отправлять")
            return
        today = datetime.now(ZoneInfo(deps.tz)).strftime("%Y-%m-%d")
        key = f"outreach:{today}"
        sent_today = int(await db.get_setting(key, "0"))
        if sent_today >= deps.outreach_limit:
            await cq.answer(f"Лимит {deps.outreach_limit} сообщений в день исчерпан — остальное руками", show_alert=True)
            return
        try:
            await tg.send_dm(row["contact"], row["draft"])
        except Exception as e:
            await cq.answer(f"Не отправилось: {str(e)[:150]}", show_alert=True)
            return
        name = Guard.name(cq.from_user)
        await db.set_setting(key, str(sent_today + 1))
        if not row["assigned_to"]:
            await db.assign(row["id"], cq.from_user.id, name)
        await db.set_status(row["id"], "contacted", cq.from_user.id, name)
        await db.schedule_followup(row["id"], deps.followup_hours)
        await edit(cq, cq.message.html_text + f"\n\n📨 отправлено — {html.escape(name)}",
                   taken_keyboard(row["id"], True, False))
        await cq.answer(f"Отправлено {row['contact']} ({sent_today + 1}/{deps.outreach_limit} за сегодня)")

    @router.callback_query(F.data.startswith("mute:"))
    async def mute_chat(cq: CallbackQuery):
        if not await ok(cq):
            return
        row = await db.get_lead(int(cq.data.split(":")[1]))
        if not row or not row["chat_key"]:
            await cq.answer("У этого лида нет чата")
            return
        await db.add_ignored("chat", row["chat_key"], row["chat_title"] or "")
        await cq.answer(f"Чат «{row['chat_title']}» в игноре. Снять: /unblock {row['chat_key']}", show_alert=True)

    return router


def followup_markup(lead_id: int):
    return followup_keyboard(lead_id)
