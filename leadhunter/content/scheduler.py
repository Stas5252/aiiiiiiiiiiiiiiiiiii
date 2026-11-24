"""Фоновые циклы контент-фабрики: публикация по расписанию и автопилот (черновик каждый день)."""
import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import features
from .factory import API_PLATFORMS, ContentFactory, error_text
from .store import errors_json

log = logging.getLogger(__name__)


async def publish_due(factory: ContentFactory, publishers: list, notify, *, limit: int = 1) -> int:
    """Один проход: не больше limit материалов, чьё время пришло (самые давние — первыми). После простоя
    пропущенные слоты выходят по одному за проход, а не пачкой. notify(text) — сообщить владельцу о результате."""
    if not await features.enabled(factory.db, "schedule_enabled"):
        return 0
    await factory.store.release_stuck()
    n = 0
    for row in await factory.store.due(limit):
        name = html.escape(row["title"] or row["topic"])
        if not publishers:
            await factory.store.schedule(row["id"], None)
            await notify(f"⚠️ #{row['id']} «{name}» не вышел: площадки не настроены (/channels). Вернул в черновики.")
            continue
        out = await factory.publish(row, publishers)
        fresh = await factory.store.get(row["id"])
        if not out.results and (not fresh or fresh["status"] not in ("draft", "scheduled")):
            continue  # материал забрал другой проход (или его успели опубликовать / удалить) — он и отчитается
        n += 1
        problems = "".join(f"\n⚠️ {html.escape(e)}" for e in out.errors)
        if out.results:
            links = ", ".join(f"{API_PLATFORMS.get(r.platform, r.platform)}" + (f' <a href="{html.escape(r.url)}">↗</a>' if r.url else "")
                              for r in out.results)
            notes = "".join(f"\n⚠️ {html.escape(r.note)}" for r in out.results if getattr(r, "note", ""))
            retry = f"\nДоотправить туда, куда не ушло: /show {row['id']} → «🔁 Доотправить»." if errors_json(fresh) else ""
            await notify(f"✅ #{row['id']} «{name}» опубликован: {links}{notes}{problems}{retry}")
        else:
            await factory.store.schedule(row["id"], None)
            await notify(f"⚠️ #{row['id']} «{name}» не опубликовался, вернул в черновики:{problems or ' причина неизвестна'}")
    return n


async def content_loop(factory: ContentFactory, get_publishers, notify) -> None:
    while True:
        try:
            await publish_due(factory, await get_publishers(), notify)
        except Exception:
            log.exception("content loop")
        await asyncio.sleep(60)


AUTOPILOT_TRIES = 3  # столько раз за день автопилот пробует сделать черновик, потом сдаётся до завтра
PLAN_DAYS = 7  # идеи плана старше недели автопилот не берёт: план уже неактуален


async def _plan_idea(factory: ContentFactory):
    """Самая давняя неиспользованная идея из /plan, чей день уже наступил (но не старше недели)."""
    now = datetime.now(timezone.utc)
    ready = []
    for r in await factory.store.recent(200, status="draft", kind="idea"):
        if not r["scheduled_at"]:
            continue
        try:
            at = datetime.fromisoformat(r["scheduled_at"])
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if now - timedelta(days=PLAN_DAYS) <= at <= now:
            ready.append((at, r["id"], r))
    return min(ready, key=lambda x: x[:2])[2] if ready else None


async def autopilot_once(factory: ContentFactory, deliver, tz_name: str, hour: int, *, now: datetime | None = None,
                         notify=None) -> int | None:
    """Если автопилот включён и сегодня ещё не приносили: взять идею из плана на сегодня (или придумать) → черновик.
    День отмечается выполненным только после черновика. Ошибка (ИИ недоступен) — владелец получает сообщение,
    попыток за день не больше AUTOPILOT_TRIES."""
    db = factory.db
    if await db.get_setting("autopilot", "0") != "1":
        return None
    now = now or datetime.now(ZoneInfo(tz_name))
    today = now.strftime("%Y-%m-%d")
    if now.hour < hour or await db.get_setting("autopilot_last") == today:
        return None
    notify = notify or getattr(factory, "notify", None)
    day, _, tries = (await db.get_setting("autopilot_fail", "") or "").partition(":")
    tries = int(tries) if day == today and tries.isdigit() else 0
    try:
        kind, topic, parent = "post", "", None
        idea = await _plan_idea(factory)
        if idea:
            kind, topic, parent = (idea["platforms"] or "post"), idea["topic"], idea["id"]
        else:
            ideas = (await factory.gen.generate("ideas", "", n=1))["ideas"]
            kind, topic = ideas[0].get("kind", "post"), ideas[0]["topic"]
        if not await features.enabled(db, "format_" + kind):
            kind = (await factory.gen.idea_kinds())[0]  # формат идеи выключен — берём первый включённый
        row = await factory.create(kind, topic, created_by="автопилот", parent_id=parent)
    except Exception as e:
        tries += 1
        last = tries >= AUTOPILOT_TRIES
        await db.set_setting("autopilot_fail", f"{today}:{tries}")
        if last:
            await db.set_setting("autopilot_last", today)
        log.warning("автопилот: попытка %s не удалась: %r", tries, e)
        if notify and (tries == 1 or last):
            why = html.escape(error_text(e, 300))  # ошибки ИИ генератор уже перевёл на простой язык
            tail = ("Сегодня черновика не будет. Сделайте сами: /post тема." if last
                    else "Попробую ещё раз через несколько минут.")
            try:
                await notify(f"⚠️ Автопилот не смог сделать черновик: {why}\n{tail}")
            except Exception:
                log.exception("автопилот: не удалось сообщить владельцу")
        return None
    await db.set_setting("autopilot_last", today)
    if parent:
        await factory.store.update(parent, status="published")  # идея использована
    await deliver(row)
    return row["id"]


async def autopilot_loop(factory: ContentFactory, deliver, tz_name: str, hour: int) -> None:
    while True:
        try:
            await autopilot_once(factory, deliver, tz_name, hour)
        except Exception:
            log.exception("autopilot")
        await asyncio.sleep(600)
