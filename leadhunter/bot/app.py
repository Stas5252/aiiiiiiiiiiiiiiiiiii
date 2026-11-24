"""Сборка диспетчера: все роутеры бота + общие предохранители.

- режим ввода (ключ в /setup, тема поста, правка) сбрасывается любой командой и сам — через 5 минут;
- упавший обработчик не молчит: ошибка уходит в лог, а человеку с доступом — короткое сообщение."""
import logging
import time
import traceback

from aiogram import BaseMiddleware, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.types import ErrorEvent, Message

from ..integrations import scrub
from .callbacks import build_callbacks
from .content import build_content_router
from .handlers import build_router
from .setup import build_onboarding_callbacks, build_setup_router

log = logging.getLogger(__name__)

STATE_TTL = 300  # секунд бот ждёт ввод после кнопки/команды, потом забывает
STAMP = "_state_at"


class StateReset(BaseMiddleware):
    """Сбрасывает режим ввода, если человек передумал (написал команду), прошло STATE_TTL или у него отозвали доступ."""

    def __init__(self, guard):
        self.guard = guard

    async def _stale(self, event, state: FSMContext) -> bool:
        if isinstance(event, Message) and (event.text or event.caption or "").startswith("/"):
            return True
        stamp = (await state.get_data()).get(STAMP)
        if stamp is None or time.time() - stamp > STATE_TTL:
            return True
        user = event.from_user
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        return user is None or not await self.guard.allowed(user, chat.id if chat else None)

    async def __call__(self, handler, event, data):
        state: FSMContext | None = data.get("state")
        if state is None:
            return await handler(event, data)
        if await state.get_state() and await self._stale(event, state):
            await state.clear()
            data["raw_state"] = None  # фильтры aiogram смотрят сюда, а не в хранилище
        entered = False
        set_state = state.set_state

        async def tracked(state=None):  # та же сигнатура, что у FSMContext.set_state
            nonlocal entered
            entered = state is not None
            await set_state(state)

        state.set_state = tracked  # отсчёт 5 минут идёт от входа в режим ввода, посторонние кнопки его не продлевают
        try:
            return await handler(event, data)
        finally:
            if entered and await state.get_state():
                await state.update_data(**{STAMP: time.time()})


def install_safety(dp: Dispatcher, deps) -> None:
    reset = StateReset(deps.guard)
    dp.message.outer_middleware(reset)
    dp.callback_query.outer_middleware(reset)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        upd, exc = event.update, event.exception
        trace = "".join(traceback.format_exception(exc))
        s = deps.extra.get("settings")
        log.error("обработчик упал (update %s): %s\n%s", upd.update_id, type(exc).__name__, scrub(trace, s) if s else trace)
        text = (f"⚠️ Не получилось выполнить: внутренняя ошибка ({type(exc).__name__}). "
                "Попробуйте ещё раз; если повторится — посмотрите лог бота.")
        try:
            if upd.message and upd.message.from_user:
                m = upd.message
                if await deps.guard.allowed(m.from_user, m.chat.id):
                    await m.answer(text, parse_mode=None)
            elif upd.callback_query:
                cq = upd.callback_query
                if await deps.guard.allowed(cq.from_user, cq.message.chat.id if cq.message else None):
                    await cq.answer(text[:200], show_alert=True)
        except Exception as e:
            log.warning("не удалось сообщить об ошибке в чат: %s", type(e).__name__)
        return True


def build_dispatcher(deps) -> Dispatcher:
    dp = Dispatcher()
    install_safety(dp, deps)
    for router in (build_router(deps), build_callbacks(deps), build_content_router(deps),
                   build_setup_router(deps), build_onboarding_callbacks(deps)):
        dp.include_router(router)
    return dp
