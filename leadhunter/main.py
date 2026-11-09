import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .bot.handlers import build_digest, build_router
from .bot.notify import TelegramNotifier
from .config import Settings
from .db import Database
from .llm import LLMClassifier
from .pipeline import Pipeline
from .sources import build_sources

log = logging.getLogger("leadhunter")


async def digest_loop(db: Database, notifier: TelegramNotifier, hour: int, tz_name: str) -> None:
    tz = ZoneInfo(tz_name)
    while True:
        now = datetime.now(tz)
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await notifier.send(await build_digest(db))
        except Exception:
            log.exception("дайджест не отправился")


async def run() -> None:
    s = Settings()
    db = Database(s.db_path)
    await db.connect()

    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    notifier = TelegramNotifier(bot, db, s.owner_ids)
    llm = None
    if s.openai_api_key:
        llm = LLMClassifier(s.openai_api_key, s.openai_base_url, s.openai_model, s.llm_max_per_hour)
    pipeline = Pipeline(db, notifier, s.score_threshold, llm, max_age_hours=s.max_age_hours,
                        llm_min_score=s.llm_min_score, profile=s.profile)

    sources = build_sources(s, db)
    log.info("источники: %s; ИИ: %s", ", ".join(x.name for x in sources) or "нет", s.openai_model if llm else "выкл")

    dp = Dispatcher()
    dp.include_router(build_router(db, sources, s.owner_ids))

    tasks = [asyncio.create_task(src.run(pipeline.handle), name=src.name) for src in sources]
    if s.digest_hour is not None:
        tasks.append(asyncio.create_task(digest_loop(db, notifier, s.digest_hour, s.digest_tz), name="digest"))
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
