"""Сборка контент-фабрики из настроек и подключение к боту (main.py и тесты)."""
import logging

from ..config import Settings
from .factory import ContentFactory
from .generator import ContentGenerator
from .images import MediaAI
from .media import MediaStore
from .publish import build_publishers
from .store import ContentStore

log = logging.getLogger(__name__)


async def build_content(s: Settings, db, bot, llm, deps, notifier) -> ContentFactory:
    store = ContentStore(db)
    await store.init()
    media = MediaStore(s.content_dir, s.miniapp_url)
    ai = MediaAI(s.openai_api_key, s.openai_base_url, image_model=s.image_model, tts_model=s.tts_model, tts_voice=s.tts_voice)
    factory = ContentFactory(store, ContentGenerator(llm, store, db, s.profile), media, ai, db)

    async def publishers() -> list:
        """Площадки: Telegram и VK задаются в боте (/channel_set), токены Threads/Instagram/YouTube — в .env."""
        ch = await store.channels()
        return build_publishers(
            bot=bot, telegram_target=ch.get("telegram"),
            vk_token=s.vk_token, vk_owner_id=int(ch["vk"]) if ch.get("vk") else None,
            threads_token=s.threads_access_token, threads_user_id=s.threads_user_id,
            ig_token=s.ig_access_token, ig_user_id=s.ig_user_id,
            yt_client_id=s.yt_client_id, yt_client_secret=s.yt_client_secret, yt_refresh_token=s.yt_refresh_token,
            public_url=media.public_url,
        )

    async def deliver_to_team(row) -> None:
        """Карточка от автопилота/планировщика: в тему «Контент», иначе владельцам в личку."""
        deliver = deps.extra.get("deliver_content")
        if not deliver:
            return
        ws = notifier.workspace
        chat_id = await ws.chat_id()
        if chat_id:
            await deliver(row, chat_id, await ws.topic("content"))
            return
        for owner in await notifier.owners():
            await deliver(row, owner, None)

    async def notify(text: str) -> None:
        await notifier.send(text, topic="content", silent=True)

    deps.extra.update(factory=factory, publishers=publishers, content_hour=s.content_hour,
                      deliver_to_team=deliver_to_team, content_notify=notify)
    return factory
