import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .bot.callbacks import build_callbacks
from .bot.guard import Guard
from .bot.handlers import BotDeps, build_router
from .bot.notify import TelegramNotifier
from .bot.workspace import Workspace
from .config import Settings
from .db import Database
from .llm import LLMClassifier
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

    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    tg_notifier = TelegramNotifier(bot, db, s.owner_ids, outreach_enabled=s.tg_outreach_limit > 0,
                                   hot_score=s.hot_score, deliver_dm=s.deliver_dm)
    notifier = MultiNotifier(tg_notifier, WebhookNotifier(s.webhook_url) if s.webhook_url else None)
    llm = None
    if s.openai_api_key:
        llm = LLMClassifier(s.openai_api_key, s.openai_base_url, s.openai_model, s.llm_max_per_hour)
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
    log.info("источники: %s; ИИ: %s", ", ".join(x.name for x in sources) or "нет", s.openai_model if llm else "выкл")

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

    tasks = [asyncio.create_task(src.run(pipeline.handle), name=src.name) for src in sources]
    tasks.append(asyncio.create_task(sla_loop(db, tg_notifier, s.sla_minutes, s.sla_min_score), name="sla"))
    tasks.append(asyncio.create_task(followup_loop(db, tg_notifier), name="followup"))
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
        await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
