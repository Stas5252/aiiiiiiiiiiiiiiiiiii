"""Мастер подключений /setup и сводка /status: ключи вводятся прямо в чате, проверяются и применяются сразу."""
import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..integrations import INTEGRATIONS, Integrations

log = logging.getLogger(__name__)

GROUPS = {"ai": "🧠 Искусственный интеллект", "publish": "📣 Площадки для публикации", "sources": "🔎 Поиск лидов"}


class SetupState(StatesGroup):
    value = State()


def build_setup_router(deps) -> Router:
    router = Router()

    def integ() -> Integrations | None:
        return deps.extra.get("integrations")

    async def owner(m) -> bool:
        return await deps.guard.is_owner(m.from_user.id)

    async def menu_text_kb():
        st = await integ().status()
        lines = ["<b>Подключения</b> — нажмите, чтобы добавить или заменить ключ. Проверяю сразу, применяю без перезапуска.\n"]
        rows = []
        for g, title in GROUPS.items():
            lines.append(f"<b>{title}</b>")
            for i in (x for x in st if x["group"] == g):
                mark = "✅" if i["set"] else "▫️"
                shown = f" <code>{html.escape(i['masked'])}</code>" if i["set"] and i["masked"] else ""
                lines.append(f"{mark} {html.escape(i['title'])}{shown}")
                rows.append([InlineKeyboardButton(text=f"{mark} {i['title'][:40]}", callback_data=f"su:{i['key']}")])
        s = deps.extra.get("settings")
        lines.append("")
        if s is not None and not (s.tg_api_id and s.tg_api_hash):
            lines.append("✈️ Telegram-чаты (чтение групп вашим аккаунтом) — только через .env: TG_API_ID, TG_API_HASH + python -m leadhunter.tg_login")
        lines.append("🤖 Куда публиковать: /channels · Mini App: /app")
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    @router.message(Command("setup"))
    async def setup(m: Message, state: FSMContext):
        if not await owner(m) or not integ():
            return
        await state.clear()
        text, kb = await menu_text_kb()
        await m.answer(text, reply_markup=kb)

    @router.message(Command("status"))
    async def status(m: Message):
        if not await deps.guard.allowed(m.from_user, m.chat.id) or not integ():
            return
        sm = integ().summary()
        f = deps.extra.get("factory")
        lines = ["<b>Состояние системы</b>"]
        lines.append(f"{'✅' if sm['ai'] else '❌'} ИИ для лидов и текстов" + (f" — {deps.llm.model}" if deps.llm else " — /setup"))
        if f is not None:
            lines.append(f"{'✅' if f.ai.images_enabled else '▫️'} Картинки ИИ · {'✅' if f.ai.tts_enabled else '▫️'} Озвучка · "
                         f"{'✅' if f.video_ai and f.video_ai.video_enabled else '▫️'} ИИ-видео"
                         + ("" if sm["media_ai"] else " — нужен ключ Gemini"))
            if sm["media_ai"]:
                lines.append("   ℹ️ На бесплатном тарифе Gemini: озвучка — 10 запросов в день, картинки и видео — только с биллингом "
                             "(aistudio.google.com → Billing). Без них обложки рисуются по шаблону.")
        lines.append(f"{'✅' if sm['miniapp'] else '❌'} Mini App «Кабинет»" + ("" if sm["miniapp"] else " — MINIAPP_TUNNEL=cloudflare в .env"))
        lines.append(f"{'✅' if sm['tg_account'] else '▫️'} Telegram-чаты через аккаунт")
        pubs = await deps.extra["publishers"]() if deps.extra.get("publishers") else []
        lines.append("📣 Публикую в: " + (", ".join(html.escape(p.label) for p in pubs) or "никуда — /channels"))
        ws = await deps.workspace.chat_id()
        lines.append(f"{'✅' if ws else '▫️'} Рабочая группа с темами" + ("" if ws else " — создайте группу-форум и напишите в ней /setup_group"))
        lines.append("\nИсточники:\n" + "\n".join(f"• {html.escape(s.status_line())}" for s in deps.sources))
        await m.answer("\n".join(lines))

    @router.callback_query(F.data.startswith("su:"))
    async def pick(cq: CallbackQuery, state: FSMContext):
        if not await owner(cq) or not integ():
            await cq.answer("Только владелец")
            return
        key = cq.data.split(":")[1]
        i = INTEGRATIONS.get(key)
        if not i:
            await cq.answer("Нет такого")
            return
        s = deps.extra.get("settings")
        missing = [INTEGRATIONS[r].title for r in i.requires if not getattr(s, r, None)]
        if missing:
            await cq.answer("Сначала: " + ", ".join(missing), show_alert=True)
            return
        await state.set_state(SetupState.value)
        await state.update_data(key=key)
        rows = [[InlineKeyboardButton(text="◀️ Отмена", callback_data="suc")]]
        if getattr(s, key, None):
            rows.insert(0, [InlineKeyboardButton(text="❌ Удалить ключ", callback_data=f"sux:{key}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        link = f'\n<a href="{i.url}">Открыть страницу, где взять</a>' if i.url else ""
        await cq.message.answer(f"<b>{html.escape(i.title)}</b>\n{html.escape(i.hint)}{link}\n\n"
                                f"Пришлите значение одним сообщением — я проверю его живым запросом и сразу применю. "
                                f"Сообщение с ключом удалю из чата.", reply_markup=kb, disable_web_page_preview=True)
        await cq.answer()

    @router.callback_query(F.data == "suc")
    async def cancel(cq: CallbackQuery, state: FSMContext):
        await state.clear()
        try:
            await cq.message.delete()
        except TelegramBadRequest:
            pass
        await cq.answer("Отменено")

    @router.callback_query(F.data.startswith("sux:"))
    async def remove(cq: CallbackQuery, state: FSMContext):
        if not await owner(cq):
            return
        key = cq.data.split(":")[1]
        await integ().unset(key)
        await state.clear()
        text, kb = await menu_text_kb()
        await cq.message.edit_text(text, reply_markup=kb)
        await cq.answer("Удалил")

    @router.message(SetupState.value, F.text)
    async def got_value(m: Message, state: FSMContext):
        data = await state.get_data()
        key = data.get("key")
        value = (m.text or "").strip().strip("`")
        try:
            await m.delete()  # ключ не должен висеть в истории чата
        except TelegramBadRequest:
            pass
        if not key or not await owner(m):
            await state.clear()
            return
        if value.startswith("/"):
            await state.clear()
            return
        wait = await m.answer("Проверяю…")
        err = await integ().set(key, value)
        await state.clear()
        if err:
            await wait.edit_text(f"❌ Не принято: {html.escape(err)}\n\nПопробуйте ещё раз: /setup")
            return
        text, kb = await menu_text_kb()
        await wait.edit_text(f"✅ {html.escape(INTEGRATIONS[key].title)} подключено и уже работает.\n\n" + text, reply_markup=kb)

    return router


def build_onboarding_callbacks(deps) -> Router:
    """Кнопки под /start: «Ниша» и «Подключения» открывают соответствующие меню."""
    router = Router()

    @router.callback_query(F.data == "ob:setup")
    async def ob_setup(cq: CallbackQuery, state: FSMContext):
        if not await deps.guard.is_owner(cq.from_user.id):
            await cq.answer("Только владелец")
            return
        await cq.answer()
        integ = deps.extra.get("integrations")
        if integ is None:
            return
        text, kb = await _menu(deps)
        await cq.message.answer(text, reply_markup=kb)

    @router.callback_query(F.data == "ob:niche")
    async def ob_niche(cq: CallbackQuery):
        from ..niches import NICHES
        cur = await deps.db.get_setting("niche", "")
        rows = [[InlineKeyboardButton(text=f"{'▶️ ' if k == cur else ''}{n.title[:38]}", callback_data=f"nz:{k}")] for k, n in NICHES.items()]
        await cq.message.answer("Выберите нишу — настрою профиль для ИИ, фразы поиска, словарь и рубрики контента:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await cq.answer()

    @router.callback_query(F.data.startswith("nz:"))
    async def pick_niche(cq: CallbackQuery):
        if not await deps.guard.allowed(cq.from_user, cq.message.chat.id if cq.message else None):
            await cq.answer("Нет доступа")
            return
        from ..niches import NICHES, apply, describe
        n = NICHES.get(cq.data.split(":")[1])
        if not n:
            await cq.answer("Нет такой")
            return
        await apply(deps.db, deps.phrases, n)
        for src in deps.sources:
            if n.yandex_queries and getattr(src, "name", "") == "yandex_maps" and hasattr(src, "queries"):
                src.queries = n.yandex_queries
        try:
            await cq.message.edit_text("Ниша выбрана:\n" + describe(n) + "\n\nУточнить описание: /profile текст")
        except TelegramBadRequest:
            pass
        await cq.answer("Готово")

    return router


async def _menu(deps):
    st = await deps.extra["integrations"].status()
    lines = ["<b>Подключения</b> — нажмите, чтобы добавить или заменить ключ.\n"]
    rows = []
    for g, title in GROUPS.items():
        lines.append(f"<b>{title}</b>")
        for i in (x for x in st if x["group"] == g):
            mark = "✅" if i["set"] else "▫️"
            lines.append(f"{mark} {html.escape(i['title'])}")
            rows.append([InlineKeyboardButton(text=f"{mark} {i['title'][:40]}", callback_data=f"su:{i['key']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)
