"""Досье на лид в боте: кнопка «🔎 Досье» под карточкой, команда /dossier N и автозапуск для горячих лидов."""
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from .. import features
from ..analysis.dossier import has_subject
from ..analysis.render import render

log = logging.getLogger(__name__)
WAIT = "Собираю досье — пришлю ответом, обычно до минуты."


def refresh_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить досье", callback_data=f"ds:{lead_id}:1")]])


async def send_to_card(bot, db, lead_id: int, text: str, **kwargs) -> bool:
    """Досье — ответом на карточку лида там, куда она была доставлена."""
    row = await db.get_lead(lead_id)
    if not row or not row["msg_chat_id"]:
        return False
    try:
        await bot.send_message(row["msg_chat_id"], text, message_thread_id=row["thread_id"], disable_web_page_preview=True,
                               reply_parameters=ReplyParameters(message_id=row["msg_id"], allow_sending_without_reply=True),
                               **kwargs)
        return True
    except Exception:
        log.exception("не удалось отправить досье на лид %s", lead_id)
        return False


def auto_dossier(service, db, bot, hot_score: int):
    """Хук конвейера: горячий лид доставлен — собрать досье и прислать ответом на карточку.
    Только когда есть что разбирать (сайт или компания с карт) и включён переключатель «Досье для горячих лидов»."""

    async def hook(lead_id: int, score: int) -> None:
        if score < hot_score or not await features.enabled(db, "dossier_auto"):
            return
        row = await db.get_lead(lead_id)
        if not row or not has_subject(row) or not service.auto_allowed():
            return

        async def done(data, error):
            if data:
                await send_to_card(bot, db, lead_id, render(lead_id, data), reply_markup=refresh_keyboard(lead_id))

        service.start(lead_id, on_done=done)

    return hook


def build_dossier_router(deps) -> Router:
    router = Router()
    db, guard = deps.db, deps.guard

    async def run(lead_id: int, force: bool, reply) -> str:
        """Готовое досье отдаёт сразу, иначе запускает сборку. Возвращает короткий ответ на нажатие."""
        service = deps.extra.get("dossier")
        if service is None:
            return "Досье недоступно"
        if not await db.get_lead(lead_id):
            return "Лид не найден"
        if not force and (stored := await db.get_dossier(lead_id)):
            await reply(render(lead_id, stored), reply_markup=refresh_keyboard(lead_id))
            return ""

        async def done(data, error):
            if data:
                await reply(render(lead_id, data), reply_markup=refresh_keyboard(lead_id))
            else:
                await reply(f"🔎 Досье на лид #{lead_id}: {error}")

        return WAIT if service.start(lead_id, force=force, on_done=done) else "Досье уже собирается — пришлю, когда будет готово."

    @router.callback_query(F.data.startswith("ds:"))
    async def dossier_button(cq: CallbackQuery):
        if not await guard.allowed(cq.from_user, cq.message.chat.id if cq.message else None):
            await cq.answer("Нет доступа")
            return
        parts = cq.data.split(":")
        if not hasattr(cq.message, "reply"):  # карточка старше 48 часов — Telegram не отдаёт само сообщение
            await cq.answer(f"Карточка слишком старая — напишите /dossier {parts[1]}", show_alert=True)
            return
        answer = await run(int(parts[1]), len(parts) > 2, cq.message.reply)
        await cq.answer(answer)

    @router.message(Command("dossier"))
    async def dossier_cmd(m: Message, command: CommandObject):
        if not await guard.allowed(m.from_user, m.chat.id):
            return
        arg = (command.args or "").strip().lstrip("#")
        if not arg.isdigit():
            await m.answer("Формат: /dossier 12 — номер лида из карточки. Или нажмите «🔎 Досье» под карточкой.")
            return
        answer = await run(int(arg), False, m.reply)
        if answer:
            await m.answer(answer)

    return router
