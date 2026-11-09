"""Локальный запуск Mini App без Telegram для разработки: python -m leadhunter.miniapp.dev [user_id]
Открывает API под указанным пользователем без проверки подписи. Только для локальной отладки."""
import asyncio
import logging
import sys

from aiohttp import web

from ..bot.guard import Guard
from ..bot.handlers import BotDeps
from ..bot.workspace import Workspace
from ..config import Settings
from ..content.runtime import build_content, build_llm
from ..db import Database
from ..phrases import PhraseProvider, default_phrases
from .server import MiniAppAPI, build_app


async def main(user_id: int, port: int) -> None:
    s = Settings()
    db = Database(s.db_path)
    await db.connect()
    guard = Guard(None, db, s.owner_ids or [user_id])
    deps = BotDeps(db, [], guard, PhraseProvider(db, default_phrases(s)), Workspace(None, db), None, s.profile)
    deps.extra["threshold"] = s.score_threshold
    from ..integrations import Integrations, load_overrides
    await load_overrides(db, s)
    llm = build_llm(s)
    factory = await build_content(s, db, None, llm, deps, None)

    async def on_change():
        from ..content.runtime import reconfigure_ai
        reconfigure_ai(s, deps, factory)

    deps.extra["integrations"] = Integrations(db, s, on_change)
    deps.extra["settings"] = s  # вкладка «Контент» и лендинги; публикация без бота недоступна
    api = MiniAppAPI(db, s.bot_token, guard, deps, [], dev_user_id=user_id)
    runner = web.AppRunner(build_app(api))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"Mini App (dev, user {user_id}): http://localhost:{port}/app")
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    asyncio.run(main(uid, port))
