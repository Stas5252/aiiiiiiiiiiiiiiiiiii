"""Публикация постов в Telegram, VK и Threads."""
import asyncio
import html
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol, Sequence

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, FSInputFile, InputMediaPhoto

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
THREADS_LIMIT = 500
VK_API = "https://api.vk.com/method/"
VK_VERSION = "5.199"
THREADS_API = "https://graph.threads.net/v1.0/"

PublicUrl = Callable[[bytes, str], Awaitable[str]]
_sleep = asyncio.sleep  # подменяется в тестах


@dataclass
class PublishResult:
    platform: str
    url: str = ""
    external_id: str = ""


class PublishError(Exception):
    pass


class Publisher(Protocol):
    platform: str
    label: str

    async def publish(self, text: str, images: Sequence[bytes] = (), video_path: str | None = None) -> PublishResult: ...


def html_to_plain(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"""<a\s[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", r"\2 (\1)", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def split_text(text: str, limit: int) -> list[str]:
    """Режет по абзацам/пробелам, чтобы не рвать слова."""
    parts = []
    while len(text) > limit:
        cut = max(text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


@asynccontextmanager
async def _http(client: httpx.AsyncClient | None):
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=120) as c:
            yield c


def _json(r: httpx.Response, service: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise PublishError(f"{service}: HTTP {r.status_code}, ответ не JSON") from None
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("error_msg") or err.get("message") if isinstance(err, dict) else err
        raise PublishError(f"{service}: {msg}")
    if r.status_code >= 400:
        raise PublishError(f"{service}: HTTP {r.status_code}")
    return data


class TelegramPublisher:
    platform = "telegram"

    def __init__(self, bot: Bot, target: str | int):
        self.bot, self.target = bot, target
        self.label = f"Telegram {target}"

    def _url(self, message_id: int, chat_username: str | None = None) -> str:
        t = str(self.target)
        if t.startswith("@"):
            return f"https://t.me/{t[1:]}/{message_id}"
        if t.startswith("-100"):
            return f"https://t.me/c/{t[4:]}/{message_id}"
        return f"https://t.me/{chat_username}/{message_id}" if chat_username else ""

    async def publish(self, text: str, images: Sequence[bytes] = (), video_path: str | None = None) -> PublishResult:
        if len(images) > 10:
            raise PublishError("Telegram: в одном посте не больше 10 картинок")
        try:
            msg = await self._send(text, images, video_path)
        except TelegramAPIError as e:
            raise PublishError(f"Telegram: {e.message}") from e
        return PublishResult("telegram", self._url(msg.message_id, getattr(msg.chat, "username", None)), str(msg.message_id))

    async def _send(self, text: str, images: Sequence[bytes], video_path: str | None):
        bot, chat = self.bot, self.target
        if not images and not video_path:
            parts = split_text(text, MESSAGE_LIMIT) or [text]
            first = await bot.send_message(chat, parts[0])
            for p in parts[1:]:
                await bot.send_message(chat, p)
            return first
        caption, *rest = split_text(text, CAPTION_LIMIT) if len(text) > CAPTION_LIMIT else [text]
        tail = text[len(caption):].lstrip() if rest else ""
        if video_path:
            first = await bot.send_video(chat, FSInputFile(video_path), caption=caption or None)
        elif len(images) == 1:
            first = await bot.send_photo(chat, BufferedInputFile(images[0], "image.jpg"), caption=caption or None)
        else:
            media = [InputMediaPhoto(media=BufferedInputFile(b, f"image{i}.jpg"), caption=caption if i == 0 and caption else None)
                     for i, b in enumerate(images)]
            first = (await bot.send_media_group(chat, media))[0]
        for p in split_text(tail, MESSAGE_LIMIT):
            await bot.send_message(chat, p)
        return first


class VKPublisher:
    platform = "vk"

    def __init__(self, token: str, owner_id: int, *, client: httpx.AsyncClient | None = None):
        self.token, self.owner_id, self.client = token, owner_id, client
        self.label = f"VK группа {owner_id}" if owner_id < 0 else f"VK стена {owner_id}"

    async def _call(self, c: httpx.AsyncClient, method: str, **params) -> dict:
        params |= {"access_token": self.token, "v": VK_VERSION}
        r = await c.post(VK_API + method, data=params)
        return _json(r, "VK").get("response") or {}

    async def _upload(self, c: httpx.AsyncClient, url: str, field: str, filename: str, content: bytes) -> dict:
        r = await c.post(url, files={field: (filename, content)})
        return _json(r, "VK upload")

    async def publish(self, text: str, images: Sequence[bytes] = (), video_path: str | None = None) -> PublishResult:
        if len(images) > 10:
            raise PublishError("VK: в одном посте не больше 10 картинок")
        owner = self.owner_id
        group = {"group_id": abs(owner)} if owner < 0 else {}
        plain = html_to_plain(text)
        attachments: list[str] = []
        try:
            async with _http(self.client) as c:
                for i, img in enumerate(images):
                    srv = await self._call(c, "photos.getWallUploadServer", **group)
                    up = await self._upload(c, srv["upload_url"], "photo", f"image{i}.jpg", img)
                    saved = (await self._call(c, "photos.saveWallPhoto", server=up["server"], photo=up["photo"],
                                              hash=up["hash"], **group))
                    p = saved[0] if isinstance(saved, list) else saved
                    attachments.append(f"photo{p.get('owner_id', owner)}_{p['id']}")
                if video_path:
                    name = (plain.splitlines() or [""])[0][:128] or "Видео"
                    srv = await self._call(c, "video.save", name=name, wallpost=0, **group)
                    await self._upload(c, srv["upload_url"], "video_file", Path(video_path).name, Path(video_path).read_bytes())
                    attachments.append(f"video{srv.get('owner_id', owner)}_{srv['video_id']}")
                params = {"owner_id": owner, "message": plain}
                if owner < 0:
                    params["from_group"] = 1
                if attachments:
                    params["attachments"] = ",".join(attachments)
                post = await self._call(c, "wall.post", **params)
        except (httpx.HTTPError, KeyError) as e:
            raise PublishError(f"VK: {e!r}") from e
        pid = post.get("post_id")
        if not pid:
            raise PublishError("VK: нет post_id в ответе")
        return PublishResult("vk", f"https://vk.com/wall{owner}_{pid}", str(pid))


class ThreadsPublisher:
    platform = "threads"
    label = "Threads"

    def __init__(self, token: str, user_id: str, *, public_url: PublicUrl | None = None,
                 client: httpx.AsyncClient | None = None):
        self.token, self.user_id, self.public_url, self.client = token, str(user_id), public_url, client

    async def _post(self, c: httpx.AsyncClient, path: str, **params) -> str:
        r = await c.post(THREADS_API + path, data=params | {"access_token": self.token})
        data = _json(r, "Threads")
        if not data.get("id"):
            raise PublishError("Threads: нет id в ответе")
        return str(data["id"])

    async def _wait_ready(self, c: httpx.AsyncClient, container: str) -> None:
        for _ in range(20):
            r = await c.get(THREADS_API + container, params={"fields": "status,error_message", "access_token": self.token})
            data = _json(r, "Threads")
            status = data.get("status")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"Threads: обработка видео не удалась ({data.get('error_message') or status})")
            await _sleep(3)
        raise PublishError("Threads: видео не обработалось за 60 секунд")

    async def publish(self, text: str, images: Sequence[bytes] = (), video_path: str | None = None) -> PublishResult:
        if (images or video_path) and self.public_url is None:
            raise PublishError("Для картинок в Threads нужен MINIAPP_URL")
        if len(images) > 20:
            raise PublishError("Threads: в карусели не больше 20 картинок")
        plain = html_to_plain(text)
        if len(plain) > THREADS_LIMIT:
            plain = plain[:THREADS_LIMIT - 1].rstrip() + "…"
        base = f"{self.user_id}/threads"
        try:
            async with _http(self.client) as c:
                if video_path:
                    url = await self.public_url(Path(video_path).read_bytes(), ".mp4")
                    cid = await self._post(c, base, media_type="VIDEO", video_url=url, text=plain)
                    await self._wait_ready(c, cid)
                elif len(images) == 1:
                    url = await self.public_url(images[0], ".jpg")
                    cid = await self._post(c, base, media_type="IMAGE", image_url=url, text=plain)
                elif images:
                    items = []
                    for img in images:
                        url = await self.public_url(img, ".jpg")
                        items.append(await self._post(c, base, media_type="IMAGE", image_url=url, is_carousel_item="true"))
                    cid = await self._post(c, base, media_type="CAROUSEL", children=",".join(items), text=plain)
                else:
                    cid = await self._post(c, base, media_type="TEXT", text=plain)
                post_id = await self._post(c, f"{self.user_id}/threads_publish", creation_id=cid)
                url = ""
                try:
                    r = await c.get(THREADS_API + post_id, params={"fields": "permalink", "access_token": self.token})
                    url = r.json().get("permalink", "")
                except (httpx.HTTPError, ValueError, AttributeError):
                    pass
        except httpx.HTTPError as e:
            raise PublishError(f"Threads: {e!r}") from e
        return PublishResult("threads", url, post_id)


def build_publishers(*, bot: Bot | None = None, telegram_target: str | int | None = None,
                     vk_token: str | None = None, vk_owner_id: int | None = None,
                     threads_token: str | None = None, threads_user_id: str | None = None,
                     public_url: PublicUrl | None = None) -> list[Publisher]:
    pubs: list[Publisher] = []
    if bot is not None and telegram_target:
        pubs.append(TelegramPublisher(bot, telegram_target))
    if vk_token and vk_owner_id:
        pubs.append(VKPublisher(vk_token, vk_owner_id))
    if threads_token and threads_user_id:
        pubs.append(ThreadsPublisher(threads_token, threads_user_id, public_url=public_url))
    return pubs
