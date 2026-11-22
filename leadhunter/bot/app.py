"""Сборка диспетчера: все роутеры бота + общие предохранители.

- режим ввода (ключ в /setup, тема поста, правка) сбрасывается любой командой и сам — через 5 минут;
- упавший обработчик не молчит: ошибка уходит в лог, а человеку с доступом — короткое сообщение."""
import logging
import time

from aiogram import BaseMiddleware, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from .callbacks import build_callbacks
from .content import build_content_router
from .handlers import build_router
from .setup import build_onboarding_callbacks, build_setup_router

log = logging.getLogger(__name__)

STATE_TTL = 300  # секунд бот ждёт ввод после кнопки/команды, потом забывает
STAMP = "_state_at"


class StateReset(BaseMiddleware):
    """Сбрасывает режим ввода, если человек передумал (написал команду) или прошло STATE_TTL."""

    async def __call__(self, handler, event, data):
        state: FSMContext | None = data.get("state")
        if state is None:
            return await handler(event, data)
        before = await state.get_state()
        if before:
            stamp = (await state.get_data()).get(STAMP)
            is_command = isinstance(event, Message) and (event.text or event.caption or "").startswith("/")
            if is_command or (stamp is not None and time.time() - stamp > STATE_TTL):
                await state.clear()
                before = None
        try:
            return await handler(event, data)
        finally:
            after = await state.get_state()
            if after and (after != before or isinstance(event, CallbackQuery) or STAMP not in await state.get_data()):
                await state.update_data(**{STAMP: time.time()})


def install_safety(dp: Dispatcher, deps) -> None:
    reset = StateReset()
    dp.message.outer_middleware(reset)
    dp.callback_query.outer_middleware(reset)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        upd, exc = event.update, event.exception
        log.error("обработчик упал (update %s): %s", upd.update_id, type(exc).__name__, exc_info=exc)
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
