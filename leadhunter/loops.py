"""Фоновые циклы: SLA (лид не взят N минут), досылка недоставленных карточек, служебные алерты, уборка базы,
follow-up (клиент не ответил), дайджесты. Правило одно: запись помечается обработанной только после того,
как сообщение действительно ушло."""
import asyncio
import html
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Database
from .bot.handlers import build_digest, build_funnel
from .bot.notify import TelegramNotifier, followup_keyboard
from .bot.workspace import message_link

log = logging.getLogger(__name__)


REDELIVER_EVERY = 5  # минут
CLEANUP_EVERY = 24 * 60  # минут
MAX_ATTEMPTS = 5


async def sla_tick(db: Database, notifier: TelegramNotifier, minutes: int, min_score: int) -> int:
    """Пинг «лид никто не взял». Только по доставленным карточкам, не на паузе, не старше трёх сроков SLA,
    не больше 5 за проход — после простоя пачка старых лидов не прилетает."""
    if await db.get_setting("paused") == "1":
        return 0
    sent = 0
    for r in await db.sla_overdue(minutes, min_score):
        age = int((datetime.now(timezone.utc) - datetime.fromisoformat(r["created_at"])).total_seconds() // 60)
        link = message_link(r["msg_chat_id"], r["thread_id"], r["msg_id"]) if r["msg_chat_id"] and r["msg_id"] else r["url"]
        head = html.escape((r["title"] or r["text"] or "")[:80])
        text = (f"⏰ <b>Лид #{r['id']} никто не взял {age} мин</b> ({r['score']}/10)\n{head}\n"
                + (f'<a href="{html.escape(link)}">Открыть</a>' if link else ""))
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👋 Беру", callback_data=f"take:{r['id']}")]])
        if await notifier.send(text, topic="hot", silent=False, reply_markup=kb):
            await db.mark_sla_pinged(r["id"])
            sent += 1
    return sent


async def redeliver_tick(db: Database, notifier: TelegramNotifier) -> int:
    """Досылает карточки, которые не ушли (сбой Telegram, не было владельца). Раньше это делал только /start."""
    if await db.get_setting("paused") == "1":
        return 0
    if not await notifier.workspace.chat_id() and not await notifier.owners():
        return 0  # слать некому — попытки не тратим
    hours = await db.get_setting("board_max_age_hours")
    try:
        board_hours = float(hours) if hours else 3.0
    except ValueError:
        board_hours = 3.0
    sent = 0
    for row in await db.pending_leads(board_max_age_hours=board_hours, max_attempts=MAX_ATTEMPTS):
        if await notifier.notify_row(row):
            sent += 1
        elif await db.bump_attempts(row["id"]) >= MAX_ATTEMPTS:
            await db.add_alert(f"undelivered:{row['id']}",
                               f"⚠️ Лид #{row['id']} не удалось отправить {MAX_ATTEMPTS} раз подряд — "
                               "он сохранён, откройте его в кабинете или через /leads.")
    return sent


async def alerts_tick(db: Database, notifier: TelegramNotifier) -> int:
    """Служебные алерты из базы (лимит ИИ, авто-игнор автора): удаляются только после отправки."""
    sent = 0
    for a in await db.alerts():
        if await notifier.send(a["text"]):
            await db.remove_alert(a["key"])
            sent += 1
    return sent


async def sla_loop(db: Database, notifier: TelegramNotifier, minutes: int, min_score: int) -> None:
    """Минутный цикл доставки: SLA, алерты, раз в 5 минут — досылка, раз в сутки — уборка базы."""
    tick = 0
    while True:
        steps = [("sla", sla_tick(db, notifier, minutes, min_score)), ("alerts", alerts_tick(db, notifier))]
        if tick % REDELIVER_EVERY == 0:
            steps.append(("досылка", redeliver_tick(db, notifier)))
        if tick % CLEANUP_EVERY == 0:
            steps.append(("уборка", db.cleanup()))
        for name, step in steps:
            try:
                await step
            except Exception:
                log.exception("цикл доставки: %s", name)
        tick += 1
        await asyncio.sleep(60)


async def followup_tick(db: Database, notifier: TelegramNotifier) -> int:
    sent = 0
    for r in await db.followups_due():
        head = html.escape((r["title"] or r["text"] or "")[:80])
        who = f" ({html.escape(r['assigned_name'])})" if r["assigned_name"] else ""
        contact = f" · {html.escape(r['contact'])}" if r["contact"] else ""
        text = (f"🔔 <b>Follow-up #{r['id']}</b>{who}: писали {r['followup_count']} раз, ответа нет\n"
                f"{head}{contact}")
        if await notifier.send(text, topic="hot", silent=False, reply_markup=followup_keyboard(r["id"])):
            await db.clear_followup(r["id"])  # не ушло — напомним на следующем проходе
            sent += 1
    return sent


async def followup_loop(db: Database, notifier: TelegramNotifier) -> None:
    while True:
        try:
            await followup_tick(db, notifier)
        except Exception:
            log.exception("followup loop")
        await asyncio.sleep(300)


def digest_settings_error(hour, tz_name: str) -> str | None:
    """Понятная причина, почему дайджест не может работать с такими настройками. None — всё в порядке."""
    try:
        ZoneInfo(tz_name)
    except Exception:
        return f"DIGEST_TZ=«{tz_name}»: такого часового пояса нет. Пример: Europe/Moscow, Asia/Yekaterinburg"
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        return f"DIGEST_HOUR=«{hour}»: нужен час от 0 до 23"
    return None


def next_digest_at(after: datetime, hour: int, tz: ZoneInfo) -> datetime:
    """Ближайшее «hour:00 по местному времени» строго после after, в UTC. Разницу считаем в UTC:
    вычитание местных времён через перевод часов даёт ошибку в час (дубль осенью, опоздание весной)."""
    after = after.astimezone(timezone.utc)
    day = after.astimezone(tz).date()
    while True:
        at = datetime.combine(day, time(hour), tzinfo=tz).astimezone(timezone.utc)
        if at > after:
            return at
        day += timedelta(days=1)


async def digest_loop(db: Database, notifier: TelegramNotifier, hour: int, tz_name: str) -> None:
    error = digest_settings_error(hour, tz_name)
    if error:
        log.error("дайджест выключен: %s", error)
        await db.add_alert("digest_settings", f"⚠️ Ежедневный дайджест выключен. {html.escape(error)}")
        return
    tz = ZoneInfo(tz_name)
    last = datetime.now(timezone.utc)
    while True:
        nxt = next_digest_at(max(last, datetime.now(timezone.utc)), hour, tz)
        while (wait := (nxt - datetime.now(timezone.utc)).total_seconds()) > 0:
            await asyncio.sleep(min(wait, 3600))  # короткими отрезками: перевод системных часов не собьёт
        last = nxt
        try:
            await notifier.send(await build_digest(db), topic="reports")
            if nxt.astimezone(tz).weekday() == 0:  # понедельник — недельная воронка
                await notifier.send(await build_funnel(db, 7, "source"), topic="reports")
        except Exception:
            log.exception("дайджест не отправился")
