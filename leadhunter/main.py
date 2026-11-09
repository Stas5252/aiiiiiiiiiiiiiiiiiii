import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .bot.callbacks import build_callbacks
from .bot.content import build_content_router
from .bot.setup import build_onboarding_callbacks, build_setup_router
from .bot.guard import Guard
from .bot.handlers import BotDeps, build_router
from .bot.notify import TelegramNotifier
from .bot.workspace import Workspace
from .config import Settings
from .content.runtime import build_content, build_llm, reconfigure_ai
from .content.scheduler import autopilot_loop, content_loop
from .db import Database
from .integrations import Integrations, load_overrides
from .loops import digest_loop, followup_loop, sla_loop
from .phrases import PhraseProvider, default_phrases
from .pipeline import Pipeline
from .sources import build_sources
from .sources.telegram_chats import TelegramChatsSource
from .webhook import MultiNotifier, WebhookNotifier

log = logging.getLogger("leadhunter")


async def run() -> None:
    s = Settings()
    db = Database(s.db_path)
    await db.connect()
    applied = await load_overrides(db, s)  # ключи, введённые через /setup или кабинет
    if applied:
        log.info("подключения из базы: %s", ", ".join(applied))

    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    tg_notifier = TelegramNotifier(bot, db, s.owner_ids, outreach_enabled=s.tg_outreach_limit > 0,
                                   hot_score=s.hot_score, deliver_dm=s.deliver_dm)
    notifier = MultiNotifier(tg_notifier, WebhookNotifier(s.webhook_url) if s.webhook_url else None)
    llm = build_llm(s)
    pipeline = Pipeline(db, notifier, s.score_threshold, llm, max_age_hours=s.max_age_hours,
                        llm_min_score=s.llm_min_score, profile=s.profile, spam_chats=s.spam_chats_per_hour)

    phrases = PhraseProvider(db, default_phrases(s))
    sources = build_sources(s, db, phrases)
    for src in sources:
        src.alert = tg_notifier.send
    tg = next((x for x in sources if isinstance(x, TelegramChatsSource)), None)
    if tg:
        tg.on_message_stats = db.bump_chat_stats
        pipeline.author_bio = tg.get_bio
    log.info("источники: %s; ИИ: %s", ", ".join(x.name for x in sources) or "нет", llm.model if llm else "выкл")

    guard = Guard(bot, db, s.owner_ids)
    deps = BotDeps(db, sources, guard, phrases, tg_notifier.workspace, llm, s.profile, s.tg_outreach_limit,
                   s.followup_hours, s.digest_tz)
    deps.extra["redeliver"] = tg_notifier.notify_row
    deps.extra["threshold"] = s.score_threshold
    if tg:
        from .discovery import ChatDiscovery
        deps.discover = ChatDiscovery(tg, db, llm, s.discover_join_per_day)

    dp = Dispatcher()
    dp.include_router(build_router(deps))
    dp.include_router(build_callbacks(deps))
    dp.include_router(build_content_router(deps))
    dp.include_router(build_setup_router(deps))
    dp.include_router(build_onboarding_callbacks(deps))
    deps.extra["pipeline"] = pipeline

    tunnel = None
    if s.miniapp_tunnel == "cloudflare" and not s.miniapp_url:
        from .tunnel import CloudflareTunnel
        tunnel = CloudflareTunnel(s.miniapp_port)
        try:
            s.miniapp_url = await tunnel.start() + "/app"
        except Exception as e:
            log.error("туннель не поднялся, Mini App выключен: %s", e)
            tunnel = None
    deps.extra["miniapp_url"] = s.miniapp_url
    factory = await build_content(s, db, bot, llm, deps, tg_notifier)

    async def on_change():
        reconfigure_ai(s, deps, factory, pipeline)

    deps.extra["integrations"] = Integrations(db, s, on_change)
    deps.extra["settings"] = s

    tasks = [asyncio.create_task(src.run(pipeline.handle), name=src.name) for src in sources]
    tasks.append(asyncio.create_task(sla_loop(db, tg_notifier, s.sla_minutes, s.sla_min_score), name="sla"))
    tasks.append(asyncio.create_task(followup_loop(db, tg_notifier), name="followup"))
    tasks.append(asyncio.create_task(content_loop(factory, deps.extra["publishers"], deps.extra["content_notify"]), name="content"))
    tasks.append(asyncio.create_task(autopilot_loop(factory, deps.extra["deliver_to_team"], s.digest_tz, s.content_hour), name="autopilot"))
    if s.digest_hour is not None:
        tasks.append(asyncio.create_task(digest_loop(db, tg_notifier, s.digest_hour, s.digest_tz), name="digest"))
    if s.miniapp_url:
        from .miniapp.server import start_miniapp
        tasks.append(asyncio.create_task(start_miniapp(s, db, bot, deps), name="miniapp"))
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        if tunnel:
            await tunnel.stop()
        await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
