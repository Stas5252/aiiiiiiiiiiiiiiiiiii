"""Оркестратор: сгенерировать → отрендерить медиа → сохранить → опубликовать / собрать пакет для ручной выкладки."""
import asyncio
import html
import json
import logging
from dataclasses import dataclass

from .generator import ContentGenerator, magnet_text
from .images import MediaAI
from .media import MediaStore
from .store import ContentStore, body_json, media_json

log = logging.getLogger(__name__)

# Куда реально умеем публиковать через API; остальное (TikTok, Дзен…) — пакетом для ручной выкладки
API_PLATFORMS = {"telegram": "Telegram", "vk": "VK", "threads": "Threads", "instagram": "Instagram", "youtube": "YouTube"}
MANUAL_PLATFORMS = {"tiktok": "TikTok", "dzen": "Дзен", "pinterest": "Pinterest"}


@dataclass
class Outcome:
    results: list  # PublishResult
    errors: list[str]


def brand_from_setting(raw: str | None):
    from .render import Brand
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = {}
    return Brand(**{k: v for k, v in data.items() if k in Brand.__dataclass_fields__})


def compose_text(row) -> str:
    """Итоговый текст публикации (HTML для Telegram; публикаторы других площадок снимают теги сами)."""
    e = html.escape
    tags = f"\n\n{e(row['hashtags'])}" if row["hashtags"] else ""
    if row["kind"] == "post":
        return e(row["body"] or "") + tags
    if row["kind"] == "magnet":
        data = body_json(row)
        return e(data.get("post") or row["caption"] or row["title"] or "") + tags
    return e(row["caption"] or row["title"] or "") + tags


class ContentFactory:
    def __init__(self, store: ContentStore, gen: ContentGenerator, media: MediaStore, ai: MediaAI, db):
        self.store = store
        self.gen = gen
        self.media = media
        self.ai = ai
        self.db = db

    async def brand(self):
        return brand_from_setting(await self.db.get_setting("brand"))

    # --- создание ---
    async def create(self, kind: str, topic: str, *, created_by: str = "", lead_id: int | None = None,
                     parent_id: int | None = None, data: dict | None = None):
        data = data or await self.gen.generate(kind, topic)
        item_id = await self.save(kind, topic, data, created_by=created_by, lead_id=lead_id, parent_id=parent_id)
        await self.render(item_id)
        return await self.store.get(item_id)

    async def save(self, kind: str, topic: str, data: dict, **kw) -> int:
        if kind == "post":
            body = data.get("body", "")
        else:
            body = json.dumps(data, ensure_ascii=False)
        return await self.store.add(kind, topic, title=data.get("title", "")[:200], body=body,
                                    caption=data.get("caption", ""), hashtags=data.get("hashtags") or [],
                                    image_prompt=data.get("image_prompt", ""), **kw)

    async def replace_data(self, item_id: int, data: dict) -> None:
        row = await self.store.get(item_id)
        body = data.get("body", "") if row["kind"] == "post" else json.dumps(data, ensure_ascii=False)
        await self.store.update(item_id, title=data.get("title", row["title"] or "")[:200], body=body,
                                caption=data.get("caption", row["caption"] or ""),
                                hashtags=data.get("hashtags") if data.get("hashtags") is not None else row["hashtags"],
                                image_prompt=data.get("image_prompt", row["image_prompt"] or ""))
        await self.render(item_id)

    def data_of(self, row) -> dict:
        if row["kind"] == "post":
            return {"title": row["title"] or "", "body": row["body"] or "", "hashtags": (row["hashtags"] or "").split(),
                    "image_prompt": row["image_prompt"] or ""}
        data = body_json(row)
        return data if isinstance(data, dict) else {}

    # --- медиа ---
    async def render(self, item_id: int, *, ai_image: bool = True) -> dict:
        from .render import render_carousel, render_cover

        row = await self.store.get(item_id)
        brand = await self.brand()
        data = self.data_of(row)
        media: dict = {}
        if row["kind"] == "carousel":
            slides = data.get("slides") or []
            media["images"] = [self.media.save(png, ".png") for png in render_carousel(slides, brand)]
        elif row["kind"] == "reel":
            try:
                media["video"] = await self._video(data, brand)
            except Exception as e:
                log.warning("видео не собралось: %s", e)
                media["error"] = str(e)[:300]
        else:
            cover = None
            if ai_image and self.ai.images_enabled and row["image_prompt"]:
                try:
                    cover = await self.ai.image(row["image_prompt"])
                except Exception as e:
                    log.warning("картинка ИИ не сгенерировалась: %s", e)
            if cover:
                media["images"] = [self.media.save(cover, ".png")]
                media["ai_cover"] = True
            else:
                subtitle = data.get("subtitle") or (data.get("body") or "").split("\n")[0][:120]
                media["images"] = [self.media.save(render_cover(row["title"] or row["topic"], subtitle, brand), ".png")]
        await self.store.update(item_id, media=media)
        return media

    async def _video(self, data: dict, brand) -> str:
        from .video import build_video

        scenes = []
        for s in data.get("scenes") or []:
            audio = None
            if self.ai.tts_enabled and s.get("voice"):
                try:
                    audio = await self.ai.speech(s["voice"])
                except Exception as e:
                    log.warning("озвучка не удалась: %s", e)
            scenes.append({"text": s.get("text", ""), "duration": float(s.get("duration") or 3), "audio": audio})
        if not scenes:
            raise RuntimeError("в сценарии нет сцен")
        name = self.media.save(b"", ".mp4")
        await build_video(scenes, brand, self.media.path(name))
        return name

    def files(self, row) -> tuple[list[bytes], str | None]:
        """Картинки (bytes) и путь к видео для публикации."""
        m = media_json(row)
        images = []
        for name in m.get("images") or []:
            try:
                images.append(self.media.read(name))
            except OSError:
                log.warning("файл %s потерян", name)
        video = self.media.path(m["video"]) if m.get("video") else None
        return images, video

    # --- публикация ---
    async def publish(self, row, publishers: list, platforms: list[str] | None = None) -> Outcome:
        text = compose_text(row)
        images, video = self.files(row)
        wanted = platforms or [p.strip() for p in (row["platforms"] or "").split(",") if p.strip()] or None
        results, errors = [], []
        for pub in publishers:
            if wanted and pub.platform not in wanted:
                continue
            try:
                results.append(await asyncio.wait_for(pub.publish(text, images, video), timeout=300))
            except Exception as e:
                errors.append(f"{pub.label}: {str(e)[:200]}")
                log.warning("публикация %s в %s не удалась: %s", row["id"], pub.label, e)
        if results:
            published = json.loads(row["published"] or "[]") + [{"platform": r.platform, "url": r.url} for r in results]
            await self.store.mark_published(row["id"], published)
        return Outcome(results, errors)

    def magnet_text(self, row) -> str:
        return magnet_text(body_json(row)) if row["kind"] == "magnet" else (row["body"] or "")
