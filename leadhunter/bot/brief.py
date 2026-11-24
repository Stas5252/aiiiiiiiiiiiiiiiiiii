"""/brief — бриф исполнителя в боте: вопросы по одному, правка любого поля и «Заполнить за меня».
Тексты вопросов и сами поля — в leadhunter/brief.py, они общие с кабинетом."""
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import brief

log = logging.getLogger(__name__)
PRIVATE_ONLY = "Бриф показываю и заполняю только в личке — напишите мне /brief там."
OWNER_ONLY = "Только владелец"


class BriefState(StatesGroup):
    answer = State()  # ждём ответ на вопрос брифа
    fill = State()    # ждём текст о себе или ссылку на сайт


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def summary_text(data: dict, *, saved: bool = False) -> str:
    n = brief.filled(data)
    head = f"<b>Бриф</b> — заполнено {n} из {len(brief.FIELDS)}."
    if saved:
        head = "✅ Сохранил. " + head
    lines = [head, "По нему ИИ отбирает заказы, пишет отклики и контент." if n
             else "Пока он пуст, ИИ опирается на короткое описание из /profile. Ответьте на вопросы — "
                  "и отклики будут с вашими ценами, кейсами и в вашем тоне.", ""]
    for f in brief.FIELDS:
        value = data.get(f.key) or ""
        short = value if len(value) <= 90 else value[:90] + "…"
        lines.append(f"✅ <b>{html.escape(f.title)}:</b> {html.escape(short)}" if value else f"▫️ {html.escape(f.title)}")
    return "\n".join(lines)


def summary_keyboard(data: dict, ai: bool) -> InlineKeyboardMarkup:
    first = next((f.key for f in brief.FIELDS if not data.get(f.key)), None)
    rows = []
    if first:
        rows.append([_btn("▶️ Ответить на вопросы" if not brief.filled(data) else "▶️ Продолжить", f"br:q:{first}")])
    if ai:
        rows.append([_btn("✨ Заполнить за меня", "br:fill")])
    edit = [_btn(("✏️ " if data.get(f.key) else "➕ ") + f.title.split(",")[0], f"br:e:{f.key}") for f in brief.FIELDS]
    rows += [edit[i:i + 2] for i in range(0, len(edit), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_text(f: brief.Field, current: str) -> str:
    n = brief.KEYS.index(f.key) + 1
    text = (f"<b>{n}/{len(brief.FIELDS)} · {html.escape(f.title)}</b>\n{html.escape(f.question)}\n"
            f"<i>Например: {html.escape(f.example)}</i>")
    if current:
        text += f"\n\nСейчас: {html.escape(current[:500])}"
    return text + "\n\nПришлите ответ одним сообщением."


def build_brief_router(deps) -> Router:
    router = Router()
    db = deps.db

    async def can_edit(event) -> str | None:
        """None — можно; иначе текст отказа."""
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if not await deps.guard.is_owner(event.from_user.id):
            return OWNER_ONLY
        if chat is None or chat.type != "private":
            return PRIVATE_ONLY
        return None

    async def show_summary(m: Message, *, saved: bool = False) -> None:
        if m.chat.type != "private":  # в группе ответ видят все её участники, а в брифе цены и клиенты
            await m.answer(PRIVATE_ONLY)
            return
        data = await brief.load(db)
        await m.answer(summary_text(data, saved=saved),
                       reply_markup=summary_keyboard(data, bool(deps.llm)) if await deps.guard.is_owner(m.chat.id) else None)

    async def ask(m: Message, state: FSMContext, key: str, chain: bool) -> None:
        f = brief.BY_KEY[key]
        current = (await brief.load(db))[key]
        row = [_btn("Пропустить ▶️" if chain else "◀️ Отмена", f"br:s:{key}" if chain else "br:done")]
        if current:
            row.append(_btn("🗑 Очистить", f"br:x:{key}:{int(chain)}"))
        rows = [row] + ([[_btn("Готово", "br:done")]] if chain else [])
        await state.set_state(BriefState.answer)
        await state.update_data(key=key, chain=chain)
        await m.answer(question_text(f, current), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def after(m: Message, state: FSMContext, key: str, chain: bool) -> None:
        """Следующий вопрос по порядку или итог."""
        i = brief.KEYS.index(key) + 1
        if chain and i < len(brief.KEYS):
            await ask(m, state, brief.KEYS[i], True)
            return
        await state.clear()
        await show_summary(m, saved=True)

    @router.message(Command("brief"))
    async def brief_cmd(m: Message, state: FSMContext):
        if not await deps.guard.allowed(m.from_user, m.chat.id):
            return
        await state.clear()
        await show_summary(m)

    @router.callback_query(F.data == "ob:brief")
    async def ob_brief(cq: CallbackQuery, state: FSMContext):
        if not await deps.guard.allowed(cq.from_user, cq.message.chat.id if cq.message else None):
            await cq.answer("Нет доступа")
            return
        await state.clear()
        await show_summary(cq.message)
        await cq.answer()

    async def field_of(cq: CallbackQuery) -> str | None:
        """Поле из кнопки «br:<действие>:<поле>[:…]»; None — отказ уже отправлен."""
        key = (cq.data.split(":") + [""])[2]
        if (no := await can_edit(cq)) or key not in brief.BY_KEY:
            await cq.answer(no or "Нет такого поля", show_alert=True)
            return None
        return key

    @router.callback_query(F.data.startswith("br:q:"))  # вопросы по порядку, начиная с этого
    @router.callback_query(F.data.startswith("br:e:"))  # поправить одно поле
    async def start_question(cq: CallbackQuery, state: FSMContext):
        if key := await field_of(cq):
            await ask(cq.message, state, key, chain=cq.data.startswith("br:q:"))
            await cq.answer()

    @router.callback_query(F.data.startswith("br:s:"))
    async def skip(cq: CallbackQuery, state: FSMContext):
        if key := await field_of(cq):
            await after(cq.message, state, key, True)
            await cq.answer()

    @router.callback_query(F.data.startswith("br:x:"))
    async def clear_field(cq: CallbackQuery, state: FSMContext):
        if key := await field_of(cq):
            await brief.save(db, {key: ""})
            await after(cq.message, state, key, cq.data.endswith(":1"))
            await cq.answer("Очистил")

    @router.callback_query(F.data == "br:done")
    async def done(cq: CallbackQuery, state: FSMContext):
        if no := await can_edit(cq):
            await cq.answer(no, show_alert=True)
            return
        await state.clear()
        await show_summary(cq.message)
        await cq.answer()

    @router.message(BriefState.answer, F.text)
    async def answer(m: Message, state: FSMContext):
        data = await state.get_data()
        key, chain = data.get("key"), bool(data.get("chain"))
        if key not in brief.BY_KEY or await can_edit(m):
            await state.clear()
            return
        try:
            await brief.save(db, {key: m.text})
        except brief.BriefError as e:
            await state.set_state(BriefState.answer)  # ждём исправленный ответ ещё 5 минут
            await m.answer(f"Не принято: {html.escape(str(e))}")
            return
        await after(m, state, key, chain)

    # --- «Заполнить за меня» ---
    @router.callback_query(F.data == "br:fill")
    async def fill_start(cq: CallbackQuery, state: FSMContext):
        if no := await can_edit(cq):
            await cq.answer(no, show_alert=True)
            return
        if not deps.llm:
            await cq.answer("ИИ не подключён — добавьте ключ в /setup", show_alert=True)
            return
        await state.set_state(BriefState.fill)
        await cq.message.answer("Пришлите одним сообщением текст о себе (можно из профиля на бирже или с сайта) "
                                "или ссылку на свой сайт. Я разложу по полям и покажу, что получилось, — "
                                "сохраню только после вашего «да».",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("◀️ Отмена", "br:done")]]))
        await cq.answer()

    @router.message(BriefState.fill, F.text)
    async def fill_text(m: Message, state: FSMContext):
        from ..content.generator import ai_error_text

        await state.clear()
        if await can_edit(m) or not deps.llm:
            return
        wait = await m.answer("Читаю…")
        s = deps.extra.get("settings")
        try:
            found = await brief.fill_from(deps.llm, m.text, proxy=getattr(s, "proxy_url", None))
        except brief.BriefError as e:
            await wait.edit_text(f"Не получилось: {html.escape(str(e))}\n\nЕщё раз: /brief")
            return
        except Exception as e:
            log.warning("бриф: ИИ не смог разложить текст (%s)", type(e).__name__)
            await wait.edit_text(f"Не получилось: {html.escape(ai_error_text(e))}\n\nЕщё раз: /brief")
            return
        found = {k: v for k, v in found.items() if v}
        if not found:
            await wait.edit_text("В тексте не нашлось ничего для брифа. Ответьте на вопросы сами: /brief")
            return
        await state.set_state(BriefState.fill)
        await state.update_data(found=found)
        lines = ["<b>Вот что я понял:</b>"]
        lines += [f"<b>{html.escape(brief.BY_KEY[k].title)}:</b> {html.escape(v[:300])}" for k, v in found.items()]
        lines.append("\nСохранить? Заполненные раньше поля заменю этими; остальные не трону. Любое поле потом можно поправить.")
        await wait.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            _btn("✅ Сохранить", "br:fillok"), _btn("◀️ Отмена", "br:done")]]))

    @router.callback_query(F.data == "br:fillok")
    async def fill_save(cq: CallbackQuery, state: FSMContext):
        if no := await can_edit(cq):
            await cq.answer(no, show_alert=True)
            return
        found = (await state.get_data()).get("found")
        await state.clear()
        if not found:
            await cq.answer("Предложение устарело — пришлите текст ещё раз: /brief", show_alert=True)
            return
        await brief.save(db, found)
        await show_summary(cq.message, saved=True)
        await cq.answer("Сохранил")

    return router
