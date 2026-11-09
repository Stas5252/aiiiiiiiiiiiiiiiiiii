import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .bot.handlers import build_router
from .bot.notify import TelegramNotifier
from .config import Settings
from .db import Database
from .llm import LLMClassifier
from .pipeline import Pipeline
from .sources import build_sources

log = logging.getLogger("leadhunter")


async def run() -> None:
    s = Settings()
    db = Database(s.db_path)
    await db.connect()

    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    notifier = TelegramNotifier(bot, db, s.owner_ids)
    llm = LLMClassifier(s.openai_api_key, s.openai_base_url, s.openai_model) if s.openai_api_key else None
    pipeline = Pipeline(db, notifier, s.score_threshold, llm)

    sources = build_sources(s)
    log.info("источники: %s", ", ".join(x.name for x in sources) or "нет")

    dp = Dispatcher()
    dp.include_router(build_router(db, sources, s.owner_ids))

    tasks = [asyncio.create_task(src.run(pipeline.handle), name=src.name) for src in sources]
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
