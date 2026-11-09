"""Фоновые циклы контент-фабрики: публикация по расписанию и автопилот (черновик каждый день)."""
import asyncio
import html
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .factory import API_PLATFORMS, ContentFactory

log = logging.getLogger(__name__)


async def publish_due(factory: ContentFactory, publishers: list, notify) -> int:
    """Один проход: всё, чьё время пришло. notify(text) — сообщить владельцу о результате."""
    n = 0
    for row in await factory.store.due():
        if not publishers:
            await factory.store.schedule(row["id"], None)
            await notify(f"⚠️ #{row['id']} «{html.escape(row['title'] or row['topic'])}» не вышел: площадки не настроены (/channels). Вернул в черновики.")
            continue
        out = await factory.publish(row, publishers)
        n += 1
        if out.results:
            links = ", ".join(f"{API_PLATFORMS.get(r.platform, r.platform)}" + (f' <a href="{html.escape(r.url)}">↗</a>' if r.url else "")
                              for r in out.results)
            notes = "".join(f"\n⚠️ {html.escape(r.note)}" for r in out.results if getattr(r, "note", ""))
            await notify(f"✅ #{row['id']} «{html.escape(row['title'] or row['topic'])}» опубликован: {links}{notes}")
        else:
            await factory.store.schedule(row["id"], None)
            await notify(f"⚠️ #{row['id']} не опубликовался, вернул в черновики:\n" + "\n".join(html.escape(e) for e in out.errors))
    return n


async def content_loop(factory: ContentFactory, get_publishers, notify) -> None:
    while True:
        try:
            await publish_due(factory, await get_publishers(), notify)
        except Exception:
            log.exception("content loop")
        await asyncio.sleep(60)


async def autopilot_once(factory: ContentFactory, deliver, tz_name: str, hour: int, *, now: datetime | None = None) -> int | None:
    """Если автопилот включён и сегодня ещё не приносили: взять идею из плана на сегодня (или придумать) → черновик."""
    db = factory.db
    if await db.get_setting("autopilot", "0") != "1":
        return None
    now = now or datetime.now(ZoneInfo(tz_name))
    today = now.strftime("%Y-%m-%d")
    if now.hour < hour or await db.get_setting("autopilot_last") == today:
        return None
    await db.set_setting("autopilot_last", today)  # сначала отметка — чтобы ошибка ИИ не зациклила попытки
    kind, topic, parent = "post", "", None
    for r in await factory.store.recent(50, status="draft", kind="idea"):
        if r["scheduled_at"] and r["scheduled_at"] <= datetime.now(timezone.utc).isoformat():
            kind, topic, parent = (r["platforms"] or "post"), r["topic"], r["id"]
            break
    if not topic:
        ideas = (await factory.gen.generate("ideas", "", n=1))["ideas"]
        if not ideas:
            return None
        kind, topic = ideas[0].get("kind", "post"), ideas[0]["topic"]
    row = await factory.create(kind, topic, created_by="автопилот", parent_id=parent)
    if parent:
        await factory.store.update(parent, status="published")
    await deliver(row)
    return row["id"]


async def autopilot_loop(factory: ContentFactory, deliver, tz_name: str, hour: int) -> None:
    while True:
        try:
            await autopilot_once(factory, deliver, tz_name, hour)
        except Exception:
            log.exception("autopilot")
        await asyncio.sleep(600)
