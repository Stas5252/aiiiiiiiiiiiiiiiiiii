"""Оркестратор: сгенерировать → отрендерить медиа → сохранить → опубликовать / собрать пакет для ручной выкладки."""
import asyncio
import html
import json
import logging
import os
import time
from dataclasses import dataclass

from .. import features
from .generator import NO_AI, ContentError, ContentGenerator, magnet_text
from .images import MediaAI
from .manual import MANUAL_HINT, manual_data
from .media import MediaStore
from .store import INTERRUPTED, STATUSES, ContentStore, body_json, errors_json, media_json

log = logging.getLogger(__name__)

# Куда реально умеем публиковать через API; остальное (TikTok, Дзен…) — пакетом для ручной выкладки
API_PLATFORMS = {"telegram": "Telegram", "vk": "VK", "threads": "Threads", "instagram": "Instagram", "youtube": "YouTube"}
MANUAL_PLATFORMS = {"tiktok": "TikTok", "dzen": "Дзен", "pinterest": "Pinterest"}
VIDEO_KINDS = ("reel", "toon", "aivideo")
VOICE_BUDGET = 300.0  # секунд на всю озвучку одного ролика: дольше ждать лимиты ИИ нет смысла


def error_text(e: BaseException, limit: int = 200) -> str:
    """Причина ошибки простыми словами: у таймаута и некоторых исключений текст пустой."""
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "не дождался ответа (таймаут)"
    return (str(e).strip() or type(e).__name__)[:limit]


@dataclass
class Outcome:
    results: list  # PublishResult
    errors: list[str]


def brand_from_setting(raw: str | None):
    from .render import Brand
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    # неверные цвета Brand сам заменит на цвета по умолчанию
    return Brand(**{k: v for k, v in data.items() if k in Brand.__dataclass_fields__ and isinstance(v, str)})


def compose_text(row) -> str:
    """Итоговый текст публикации (HTML для Telegram; публикаторы других площадок снимают теги сами)."""
    e = html.escape
    tags = f"\n\n{e(row['hashtags'])}" if row["hashtags"] else ""
    if row["kind"] == "post":
        return e(row["body"] or "") + tags
    if row["kind"] == "magnet":
        data = body_json(row)
        post = data.get("post") if isinstance(data, dict) else ""
        # правка владельца («✏️ Текст», кабинет) ложится в caption — она главнее анонса от ИИ
        return e(str(row["caption"] or post or row["title"] or "")) + tags
    return e(row["caption"] or row["title"] or "") + tags


class _Voice:
    """Озвучка одного ролика: после первой ошибки или исчерпанного времени больше не дёргаем ИИ — ролик выйдет без звука."""

    def __init__(self, ai, budget: float = VOICE_BUDGET):
        self.ai, self.budget = ai, budget
        self.spent, self.off, self.used = 0.0, "", False

    async def say(self, text: str) -> bytes | None:
        if not text or not str(text).strip() or not self.ai.tts_enabled or self.off:
            return None
        started = time.monotonic()
        try:
            audio = await self.ai.speech(str(text))
        except Exception as e:
            log.warning("озвучка не удалась: %s", e)
            self.off = error_text(e, 160)
            return None
        self.spent += time.monotonic() - started
        if self.spent > self.budget:
            self.off = "ИИ отвечает слишком медленно (лимит запросов)"
        self.used = self.used or bool(audio)
        return audio

    def note(self) -> str:
        return f"ролик без озвучки — {self.off}. Попробуйте пересобрать позже." if self.off else ""


class ContentFactory:
    def __init__(self, store: ContentStore, gen: ContentGenerator, media: MediaStore, ai, db, video_ai=None):
        self.store = store
        self.gen = gen
        self.media = media
        self.ai = ai  # картинки и озвучка: Gemini или OpenAI (общий интерфейс image/speech/*_enabled)
        self.video_ai = video_ai  # ИИ-видео через Gemini Omni
        self.notify = None  # async (text) — сообщить владельцу; ставит runtime.build_content
        self.db = db

    async def brand(self):
        return brand_from_setting(await self.db.get_setting("brand"))

    # --- создание ---
    async def create(self, kind: str, topic: str, *, created_by: str = "", lead_id: int | None = None,
                     parent_id: int | None = None, data: dict | None = None):
        if not await features.enabled(self.db, "format_" + kind):
            raise ValueError("Этот формат выключен в настройках контента")
        if not data and not self.gen.available:
            # без ИИ: «Заголовок | слайд | слайд» — так же, как в боте, работает и из кабинета
            data = manual_data(kind, topic)
            if not data:
                raise ContentError(f"{NO_AI} {MANUAL_HINT}")
            topic = data["title"]
        data = data or await self.gen.generate(kind, topic)
        item_id = await self.save(kind, topic, data, created_by=created_by, lead_id=lead_id, parent_id=parent_id)
        await self.render(item_id)
        return await self.store.get(item_id)

    async def save(self, kind: str, topic: str, data: dict, **kw) -> int:
        data = self.gen.clean(kind, data)  # и ответ ИИ, и ручной ввод: только ожидаемые типы, пустое — отказ
        if kind == "post":
            body = data["body"]
        else:
            body = json.dumps(data, ensure_ascii=False)
        # у лид-магнита анонс для соцсетей живёт в caption: его правит владелец и его же публикуем
        caption = data.get("post") or data.get("caption", "") if kind == "magnet" else data.get("caption", "")
        return await self.store.add(kind, topic, title=data.get("title", "")[:200], body=body,
                                    caption=caption, hashtags=data.get("hashtags") or [],
                                    image_prompt=data.get("image_prompt", ""), **kw)

    async def replace_data(self, item_id: int, data: dict) -> None:
        row = await self.store.get(item_id)
        data = self.gen.clean(row["kind"], data)
        body = data["body"] if row["kind"] == "post" else json.dumps(data, ensure_ascii=False)
        caption = data.get("caption") or row["caption"] or ""
        if row["kind"] == "magnet":
            caption = data.get("post") or caption
        await self.store.update(item_id, title=(data.get("title") or row["title"] or "")[:200], body=body,
                                caption=caption,
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
        from .render import render_carousel, render_cover, same_line

        row = await self.store.get(item_id)
        if row["kind"] == "aivideo" and not await features.enabled(self.db, "format_aivideo"):
            # старый черновик при выключенном ИИ-видео: платных запросов не делаем, готовое видео не трогаем
            return {**media_json(row), "error": "ИИ-видео выключено в настройках — включите его в кабинете, чтобы пересобрать"}
        brand = await self.brand()
        data = self.data_of(row)
        media: dict = {}
        if row["kind"] == "carousel":
            slides = data.get("slides") or []
            media["images"] = [self.media.save(png, ".png") for png in render_carousel(slides, brand)]
        elif row["kind"] == "reel":
            voice = _Voice(self.ai)
            try:
                media["video"] = await self._video(data, brand, voice)
                if voice.note():
                    media["note"] = voice.note()
            except Exception as e:
                log.warning("видео не собралось: %s", e)
                media["error"] = error_text(e, 300)
        elif row["kind"] == "toon":
            voice = _Voice(self.ai)
            try:
                media["video"] = await self._toon(data, brand, voice)
                if voice.note():
                    media["note"] = voice.note()
            except Exception as e:
                log.warning("мультик не собрался: %s", e)
                media["error"] = error_text(e, 300)
        elif row["kind"] == "aivideo":
            voice = _Voice(self.ai)
            try:
                media["video"] = await self._aivideo(data, voice)
                if voice.note():
                    media["note"] = voice.note()
            except Exception as e:
                log.warning("ИИ-видео не собралось: %s", e)
                media["error"] = error_text(e, 300)
        else:
            cover = None
            if ai_image and self.ai.images_enabled and row["image_prompt"]:
                try:
                    cover = await self.ai.image(row["image_prompt"])
                except Exception as e:
                    log.warning("картинка ИИ не сгенерировалась: %s", e)
            if cover:
                media["images"] = [self.media.save(cover, ".jpg" if cover[:3] == b"\xff\xd8\xff" else ".png")]
                media["ai_cover"] = True
            else:
                title = row["title"] or row["topic"]
                # подзаголовок — первая строка текста, которая не повторяет заголовок (иначе на обложке одно и то же дважды)
                lines = [ln.strip() for ln in str(data.get("body") or "").split("\n") if ln.strip()]
                subtitle = data.get("subtitle") or next((ln for ln in lines if not same_line(title, ln)), "")
                media["images"] = [self.media.save(render_cover(title, str(subtitle)[:120], brand), ".png")]
        old = media_json(row)
        await self.store.update(item_id, media=media)
        for name in [*(old.get("images") or []), old.get("video")]:
            if name and name not in (media.get("images") or []) and name != media.get("video"):
                self.media.remove(name)  # прошлые картинки и видео этого материала больше не нужны
        return media

    async def _build(self, voice: _Voice, scenes: list[dict], build) -> str:
        """Собрать ролик в новый файл. Не получилось — пустого .mp4 на диске не остаётся."""
        if voice.off:
            for s in scenes:  # озвучка оборвалась на середине — наполовину немой ролик хуже немого
                s["audio"] = None
        name = self.media.save(b"", ".mp4")
        path = self.media.path(name)
        try:
            await build(path)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise RuntimeError("видео получилось пустым")
        except BaseException:
            self.media.remove(name)
            raise
        return name

    async def _video(self, data: dict, brand, voice: _Voice | None = None) -> str:
        from .video import build_video

        voice = voice or _Voice(self.ai)
        scenes = []
        for s in data.get("scenes") or []:
            scenes.append({"text": s.get("text", ""), "duration": float(s.get("duration") or 3),
                           "audio": await voice.say(s.get("voice", ""))})
        if not scenes:
            raise RuntimeError("в сценарии нет сцен")
        return await self._build(voice, scenes, lambda path: build_video(scenes, brand, path))

    async def _toon(self, data: dict, brand, voice: _Voice | None = None) -> str:
        from .toon import build_toon, normalize_script

        voice = voice or _Voice(self.ai)
        script = normalize_script(data)
        for scene in script["scenes"]:
            line = scene.get("line") or {}
            scene["audio"] = await voice.say(line.get("text", ""))
        return await self._build(voice, script["scenes"], lambda path: build_toon(script, brand, path))

    async def _aivideo(self, data: dict, voice: _Voice | None = None) -> str:
        from .aivideo import assemble

        if not self.video_ai or not self.video_ai.video_enabled:
            raise RuntimeError("Чтобы нейросеть рисовала видео, подключите ключ Gemini в /setup")
        voice = voice or _Voice(self.ai)
        clips = []
        for clip in data.get("clips") or []:
            video = await self.video_ai.video(clip["prompt"], aspect="9:16")
            if not video:
                raise RuntimeError("Gemini не вернул видео")
            clips.append({"video": video, "audio": await voice.say(clip.get("voice", ""))})
        if not clips:
            raise RuntimeError("нет клипов")
        return await self._build(voice, clips, lambda path: assemble([(c["video"], c["audio"]) for c in clips], path))

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
        if video and (not os.path.isfile(video) or os.path.getsize(video) == 0):
            log.warning("видео %s потеряно или пустое", m["video"])
            video = None
        return images, video

    def not_ready(self, row) -> str:
        """Почему материал нельзя публиковать как есть (пусто — можно)."""
        images, video = self.files(row)
        if row["kind"] in VIDEO_KINDS and not video:
            why = media_json(row).get("error") or "файла нет"
            return f"видео не собрано ({why}) — пересоберите ролик кнопкой «♻️ Пересобрать»"
        if row["kind"] == "carousel" and not images:
            return "слайды не нарисованы — пересоберите карусель кнопкой «♻️ Пересобрать»"
        if row["kind"] == "idea":
            return "это идея, а не готовый материал — сначала сделайте из неё пост"
        return ""

    # --- публикация ---
    async def publish(self, row, publishers: list, platforms: list[str] | None = None) -> Outcome:
        """Публикует материал один раз: запись атомарно «захватывается», каждая площадка записывается сразу после
        успеха, а при повторе уже вышедшие площадки пропускаются (доотправляется только то, что не ушло)."""
        item_id = row["id"]
        if not await self.store.claim(item_id):
            fresh = await self.store.get(item_id)
            if not fresh:
                return Outcome([], ["материал не найден"])
            reason = {"publishing": "уже публикуется — подождите результат",
                      "published": "уже опубликован — повторно не отправляю",
                      "rejected": "удалён"}.get(fresh["status"], STATUSES.get(fresh["status"], fresh["status"]))
            return Outcome([], [f"#{item_id}: {reason}"])
        before = row["status"] if row["status"] in ("draft", "scheduled", "published") else "draft"
        results, errors, failed, done = [], [], [], []
        owed, sent = [], set()  # owed — куда не ушло в прошлый раз (только при доотправке)
        finished = looped = False
        try:
            row = await self.store.get(item_id)
            done = [p for p in json.loads(row["published"] or "[]") if isinstance(p, dict)]
            if before != "published" and done:
                before = "published"
            if done:
                owed = errors_json(row)
            # доотправка идёт только туда, куда не ушло, — не на площадки, подключённые позже. Если прошлую
            # публикацию оборвали (INTERRUPTED, площадка не названа), оставшиеся неизвестны: шлём на все, где материала нет
            only = {f["platform"] for f in owed} if owed and all(f.get("platform") for f in owed) else None
            problem = self.not_ready(row)
            if problem:
                errors.append(problem)
                finished = True
                return Outcome(results, errors)
            text = compose_text(row)
            images, video = self.files(row)
            wanted = platforms or [p.strip() for p in (row["platforms"] or "").split(",") if p.strip()] or None
            sent = {p.get("platform") for p in done}
            for f in owed:
                if f.get("platform") and f["platform"] not in sent and all(p.platform != f["platform"] for p in publishers):
                    errors.append(f"{f.get('label') or f['platform']}: площадка не подключена")
            for pub in publishers:
                if (wanted and pub.platform not in wanted) or pub.platform in sent or (only is not None and pub.platform not in only):
                    continue
                if not await features.enabled(self.db, "publish_" + pub.platform):
                    errors.append(f"{pub.label}: публикация выключена в настройках")
                    continue
                try:
                    with self.media.temporary():
                        res = await asyncio.wait_for(pub.publish(text, images, video), timeout=300)
                except Exception as e:
                    why = error_text(e)
                    errors.append(f"{pub.label}: {why}")
                    failed.append({"platform": pub.platform, "label": pub.label, "error": why})
                    log.warning("публикация %s в %s не удалась: %s", item_id, pub.label, e)
                    continue
                results.append(res)
                sent.add(res.platform)
                done.append({"platform": res.platform, "url": res.url})
                await self.store.add_published(item_id, done[-1])  # сразу: перезапуск не отправит сюда второй раз
            finished = looped = True
            if not results and not errors:
                errors.append("отправлять некуда: все подключённые площадки этот материал уже получили")
            return Outcome(results, errors)
        finally:
            if not finished and done and not failed:
                failed.append(INTERRUPTED)  # оборвали на середине: остальное можно доотправить
            # куда не ушло в прошлый раз и в этот раз не пробовали (площадка выключена, отключена, материал не готов) —
            # остаётся в списке, иначе доотправить туда будет уже нельзя
            tried = sent | {f["platform"] for f in failed}
            failed += [f for f in owed if f not in failed and (f["platform"] not in tried if f.get("platform") else not looped)]
            # что-то вышло (сейчас или раньше) — материал опубликован; не ушедшие площадки остаются в publish_errors
            # и доотправляются повторным «Опубликовать». Ничего не вышло — возвращаем прежний статус.
            await asyncio.shield(self.store.finish_publish(item_id, "published" if done else before, failed))

    def magnet_text(self, row) -> str:
        return magnet_text(body_json(row)) if row["kind"] == "magnet" else (row["body"] or "")
