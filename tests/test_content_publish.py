import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, User

from leadhunter.content import publish as pub
from leadhunter.content.publish import (InstagramPublisher, PublishError, TelegramPublisher, ThreadsPublisher, VKPublisher,
                                        YouTubePublisher, build_publishers, html_to_plain)


def test_html_to_plain():
    src = '<b>Привет</b> <i>мир</i><br>строка 2</p>3 &amp; 4 <a href="https://x.ru/a?b=1">ссылка</a>'
    assert html_to_plain(src) == "Привет мир\nстрока 2\n3 & 4 ссылка (https://x.ru/a?b=1)"


def form(req: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------- VK ----------

def vk_handler(calls, fail_on=None):
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        calls.append((name, req))
        if name == fail_on:
            return httpx.Response(200, json={"error": {"error_code": 15, "error_msg": "Access denied: no access to call this method"}})
        if name == "photos.getWallUploadServer":
            return httpx.Response(200, json={"response": {"upload_url": "https://upload.vk.com/photo", "album_id": 1}})
        if name == "upload.vk.com" or req.url.host == "upload.vk.com":
            return httpx.Response(200, json={"server": 9, "photo": "[{}]", "hash": "h"})
        if name == "photos.saveWallPhoto":
            return httpx.Response(200, json={"response": [{"id": 456, "owner_id": 777}]})
        if name == "wall.post":
            return httpx.Response(200, json={"response": {"post_id": 99}})
        raise AssertionError(name)
    return handler


async def test_vk_community_post_with_images():
    calls = []
    async with make_client(vk_handler(calls)) as c:
        res = await VKPublisher("tok", -123, client=c).publish("<b>Заголовок</b><br>текст", [b"img1", b"img2"])
    assert res.url == "https://vk.com/wall-123_99" and res.external_id == "99"
    names = [n for n, _ in calls]
    assert names == ["photos.getWallUploadServer", "photo", "photos.saveWallPhoto"] * 2 + ["wall.post"]
    assert form(calls[0][1])["group_id"] == "123"
    save = form(calls[2][1])
    assert (save["server"], save["hash"], save["group_id"]) == ("9", "h", "123")
    assert b'name="photo"' in calls[1][1].content and b"img1" in calls[1][1].content
    wall = form(calls[-1][1])
    assert wall["attachments"] == "photo777_456,photo777_456"
    assert wall["message"] == "Заголовок\nтекст" and wall["from_group"] == "1" and wall["owner_id"] == "-123"
    assert wall["v"] == "5.199" and wall["access_token"] == "tok"


async def test_vk_user_wall_text_only():
    calls = []
    async with make_client(vk_handler(calls)) as c:
        res = await VKPublisher("tok", 55, client=c).publish("просто текст")
    wall = form(calls[0][1])
    assert [n for n, _ in calls] == ["wall.post"]
    assert "from_group" not in wall and "attachments" not in wall
    assert res.url == "https://vk.com/wall55_99"


async def test_vk_video(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"VIDEO")
    calls = []

    def handler(req):
        name = req.url.path.rsplit("/", 1)[-1]
        calls.append((name, req))
        if name == "video.save":
            return httpx.Response(200, json={"response": {"upload_url": "https://upload.vk.com/video", "video_id": 5, "owner_id": -123}})
        if req.url.host == "upload.vk.com":
            return httpx.Response(200, json={"video_id": 5})
        return httpx.Response(200, json={"response": {"post_id": 7}})

    async with make_client(handler) as c:
        res = await VKPublisher("tok", -123, client=c).publish("Название ролика\nописание", video_path=str(video))
    save = form(calls[0][1])
    assert save["name"] == "Название ролика" and save["wallpost"] == "0" and save["group_id"] == "123"
    assert b'name="video_file"' in calls[1][1].content and b"VIDEO" in calls[1][1].content
    assert form(calls[2][1])["attachments"] == "video-123_5"
    assert res.url == "https://vk.com/wall-123_7"


async def test_vk_error():
    async with make_client(vk_handler([], fail_on="wall.post")) as c:
        with pytest.raises(PublishError, match="Access denied"):
            await VKPublisher("tok", -1, client=c).publish("x")


# ---------- Threads ----------

def threads_handler(calls, statuses=("FINISHED",)):
    statuses = list(statuses)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v1.0/")
        body = form(req) if req.method == "POST" else dict(req.url.params)
        calls.append((req.method, path, body))
        if req.method == "POST" and path == "U1/threads":
            return httpx.Response(200, json={"id": f"c{sum(1 for m, p, _ in calls if p == 'U1/threads')}"})
        if path == "U1/threads_publish":
            return httpx.Response(200, json={"id": "post1"})
        if path == "post1":
            return httpx.Response(200, json={"permalink": "https://www.threads.net/@me/post/abc"})
        if body.get("fields") == "status,error_message":
            return httpx.Response(200, json={"status": statuses.pop(0) if len(statuses) > 1 else statuses[0]})
        raise AssertionError(path)
    return handler


async def fake_public(data: bytes, ext: str) -> str:
    return f"https://cdn.example/{len(data)}{ext}"


async def test_threads_text_truncated():
    calls = []
    async with make_client(threads_handler(calls)) as c:
        res = await ThreadsPublisher("tok", "U1", client=c).publish("<b>" + "а" * 600 + "</b>")
    method, path, body = calls[0]
    assert path == "U1/threads" and body["media_type"] == "TEXT" and len(body["text"]) == 500 and body["text"].endswith("…")
    assert calls[1][1:] == ("U1/threads_publish", {"creation_id": "c1", "access_token": "tok"})
    assert res.url == "https://www.threads.net/@me/post/abc" and res.external_id == "post1"


async def test_threads_single_image():
    calls = []
    async with make_client(threads_handler(calls)) as c:
        await ThreadsPublisher("tok", "U1", public_url=fake_public, client=c).publish("t", [b"12345"])
    assert calls[0][2]["media_type"] == "IMAGE" and calls[0][2]["image_url"] == "https://cdn.example/5.jpg"


async def test_threads_carousel():
    calls = []
    async with make_client(threads_handler(calls)) as c:
        res = await ThreadsPublisher("tok", "U1", public_url=fake_public, client=c).publish("карусель", [b"a", b"bb", b"ccc"])
    bodies = [b for _, p, b in calls if p == "U1/threads"]
    assert [b["media_type"] for b in bodies] == ["IMAGE", "IMAGE", "IMAGE", "CAROUSEL"]
    assert all(b["is_carousel_item"] == "true" for b in bodies[:3]) and "text" not in bodies[0]
    assert bodies[3]["children"] == "c1,c2,c3" and bodies[3]["text"] == "карусель"
    assert [p for _, p, _ in calls][-2:] == ["U1/threads_publish", "post1"]
    assert res.url.endswith("/abc")


async def test_threads_video_polls_status(monkeypatch, tmp_path):
    sleeps = []

    async def no_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(pub, "_sleep", no_sleep)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"VID")
    calls = []
    async with make_client(threads_handler(calls, ["IN_PROGRESS", "FINISHED"])) as c:
        await ThreadsPublisher("tok", "U1", public_url=fake_public, client=c).publish("видео", video_path=str(video))
    assert calls[0][2]["media_type"] == "VIDEO" and calls[0][2]["video_url"] == "https://cdn.example/3.mp4"
    assert [p for _, p, _ in calls] == ["U1/threads", "c1", "c1", "U1/threads_publish", "post1"]
    assert sleeps == [3]


async def test_threads_video_error_status(monkeypatch, tmp_path):
    async def no_sleep(s):
        pass
    monkeypatch.setattr(pub, "_sleep", no_sleep)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    async with make_client(threads_handler([], ["ERROR"])) as c:
        with pytest.raises(PublishError, match="видео"):
            await ThreadsPublisher("tok", "U1", public_url=fake_public, client=c).publish("v", video_path=str(video))


async def test_threads_needs_public_url_for_images():
    with pytest.raises(PublishError, match="MINIAPP_URL"):
        await ThreadsPublisher("tok", "U1").publish("x", [b"1"])


async def test_threads_api_error():
    def handler(req):
        return httpx.Response(400, json={"error": {"message": "Invalid token", "code": 190}})
    async with make_client(handler) as c:
        with pytest.raises(PublishError, match="Invalid token"):
            await ThreadsPublisher("tok", "U1", client=c).publish("x")


# ---------- Telegram ----------

class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 10

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    def _msg(self, chat_id, **kw) -> Message:
        self.next_id += 1
        chat = Chat(id=chat_id, type="channel") if isinstance(chat_id, int) else Chat(id=-1, type="channel", username=chat_id[1:])
        return Message(message_id=self.next_id, date=datetime.now(timezone.utc), chat=chat, **kw)

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        data = method.model_dump(exclude_none=True)
        self.calls.append((name, data))
        if name == "GetMe":
            return User(id=1, is_bot=True, first_name="bot", username="testbot")
        if name == "SendMediaGroup":
            return [self._msg(data["chat_id"]) for _ in data["media"]]
        if name in ("SendMessage", "SendPhoto", "SendVideo"):
            return self._msg(data["chat_id"], text=data.get("text"))
        raise AssertionError(f"unmocked {name}")

    @property
    def names(self):
        return [n for n, _ in self.calls]


@pytest.fixture
def tg():
    session = FakeSession()
    return session, Bot("123:ABC", session=session)


async def test_tg_text_and_url(tg):
    session, bot = tg
    res = await TelegramPublisher(bot, "@chan").publish("<b>hi</b>")
    assert session.names == ["SendMessage"] and session.calls[0][1]["text"] == "<b>hi</b>"
    assert res.url == "https://t.me/chan/11" and res.external_id == "11"


async def test_tg_private_channel_url_and_plain_id(tg):
    _, bot = tg
    assert (await TelegramPublisher(bot, -1001234567).publish("x")).url == "https://t.me/c/1234567/11"
    assert (await TelegramPublisher(bot, 42).publish("x")).url == ""


async def test_tg_long_text_split(tg):
    session, bot = tg
    await TelegramPublisher(bot, "@chan").publish("слово " * 1000)
    assert session.names == ["SendMessage"] * 2
    assert all(len(d["text"]) <= 4096 for _, d in session.calls)


async def test_tg_one_image(tg):
    session, bot = tg
    await TelegramPublisher(bot, "@chan").publish("подпись", [b"img"])
    assert session.names == ["SendPhoto"] and session.calls[0][1]["caption"] == "подпись"


async def test_tg_media_group(tg):
    session, bot = tg
    res = await TelegramPublisher(bot, "@chan").publish("подпись", [b"1", b"2", b"3"])
    assert session.names == ["SendMediaGroup"]
    media = session.calls[0][1]["media"]
    assert len(media) == 3 and media[0]["caption"] == "подпись" and "caption" not in media[1]
    assert res.url == "https://t.me/chan/11"


async def test_tg_video(tg, tmp_path):
    session, bot = tg
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    await TelegramPublisher(bot, "@chan").publish("ролик", video_path=str(video))
    assert session.names == ["SendVideo"] and session.calls[0][1]["caption"] == "ролик"


async def test_tg_long_caption_split(tg):
    session, bot = tg
    text = "слово " * 400
    res = await TelegramPublisher(bot, "@chan").publish(text, [b"img"])
    assert session.names == ["SendPhoto", "SendMessage"]
    caption, rest = session.calls[0][1]["caption"], session.calls[1][1]["text"]
    assert len(caption) <= 1024 and " ".join([caption, rest]).split() == text.split()
    assert res.url == "https://t.me/chan/11"


async def test_tg_error_wrapped():
    from aiogram.exceptions import TelegramBadRequest

    class Bad(FakeSession):
        async def make_request(self, bot, method, timeout=None):
            raise TelegramBadRequest(method=method, message="chat not found")

    with pytest.raises(PublishError, match="chat not found"):
        await TelegramPublisher(Bot("123:ABC", session=Bad()), "@nope").publish("x")


def test_build_publishers(tg):
    _, bot = tg
    assert build_publishers() == []
    ps = build_publishers(bot=bot, telegram_target="@c", vk_token="t", vk_owner_id=-1, threads_token="t", threads_user_id="1")
    assert [p.platform for p in ps] == ["telegram", "vk", "threads"]
    assert [p.platform for p in build_publishers(vk_token="t", vk_owner_id=-1)] == ["vk"]


# ---------- Instagram ----------

def ig_handler(calls, statuses=("FINISHED",), permalink_ok=True):
    statuses = list(statuses)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v21.0/")
        body = form(req) if req.method == "POST" else dict(req.url.params)
        calls.append((req.method, path, body))
        if req.method == "POST" and path == "IG1/media":
            return httpx.Response(200, json={"id": f"c{sum(1 for m, p, _ in calls if p == 'IG1/media')}"})
        if path == "IG1/media_publish":
            return httpx.Response(200, json={"id": "m1"})
        if path == "m1":
            if not permalink_ok:
                return httpx.Response(500, text="oops")
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/xyz/"})
        if body.get("fields") == "status_code,status":
            return httpx.Response(200, json={"status_code": statuses.pop(0) if len(statuses) > 1 else statuses[0]})
        raise AssertionError(path)
    return handler


async def test_instagram_single_image():
    calls = []
    async with make_client(ig_handler(calls)) as c:
        res = await InstagramPublisher("tok", "IG1", public_url=fake_public, client=c).publish("<b>подпись</b>", [b"12345"])
    assert calls[0] == ("POST", "IG1/media", {"image_url": "https://cdn.example/5.jpg", "caption": "подпись", "access_token": "tok"})
    assert calls[1][1:] == ("IG1/media_publish", {"creation_id": "c1", "access_token": "tok"})
    assert res.url == "https://www.instagram.com/p/xyz/" and res.external_id == "m1" and res.platform == "instagram"


async def test_instagram_caption_truncated_and_permalink_failure_ignored():
    calls = []
    async with make_client(ig_handler(calls, permalink_ok=False)) as c:
        res = await InstagramPublisher("tok", "IG1", public_url=fake_public, client=c).publish("а" * 2500, [b"1"])
    caption = calls[0][2]["caption"]
    assert len(caption) == 2200 and caption.endswith("…")
    assert res.url == "" and res.external_id == "m1"


async def test_instagram_carousel():
    calls = []
    async with make_client(ig_handler(calls)) as c:
        res = await InstagramPublisher("tok", "IG1", public_url=fake_public, client=c).publish("карусель", [b"a", b"bb", b"ccc"])
    bodies = [b for _, p, b in calls if p == "IG1/media"]
    assert len(bodies) == 4
    assert all(b["is_carousel_item"] == "true" and "caption" not in b for b in bodies[:3])
    assert bodies[3]["media_type"] == "CAROUSEL" and bodies[3]["children"] == "c1,c2,c3" and bodies[3]["caption"] == "карусель"
    assert [p for _, p, _ in calls][-2:] == ["IG1/media_publish", "m1"]
    assert calls[-2][2]["creation_id"] == "c4"
    assert res.url.endswith("/xyz/")


async def test_instagram_reels_polls_status(monkeypatch, tmp_path):
    sleeps = []

    async def no_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(pub, "_sleep", no_sleep)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"VID")
    calls = []
    async with make_client(ig_handler(calls, ["IN_PROGRESS", "FINISHED"])) as c:
        await InstagramPublisher("tok", "IG1", public_url=fake_public, client=c).publish("рилс", video_path=str(video))
    body = calls[0][2]
    assert body["media_type"] == "REELS" and body["video_url"] == "https://cdn.example/3.mp4"
    assert body["share_to_feed"] == "true" and body["caption"] == "рилс"
    assert [p for _, p, _ in calls] == ["IG1/media", "c1", "c1", "IG1/media_publish", "m1"]
    assert sleeps == [3]


async def test_instagram_video_error_status(monkeypatch, tmp_path):
    async def no_sleep(s):
        pass
    monkeypatch.setattr(pub, "_sleep", no_sleep)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    async with make_client(ig_handler([], ["ERROR"])) as c:
        with pytest.raises(PublishError, match="видео"):
            await InstagramPublisher("tok", "IG1", public_url=fake_public, client=c).publish("v", video_path=str(video))


async def test_instagram_text_only_rejected():
    with pytest.raises(PublishError, match="картинку или видео"):
        await InstagramPublisher("tok", "IG1", public_url=fake_public).publish("только текст")


async def test_instagram_needs_public_url():
    with pytest.raises(PublishError, match="MINIAPP_URL"):
        await InstagramPublisher("tok", "IG1").publish("x", [b"1"])


async def test_instagram_api_error():
    def handler(req):
        return httpx.Response(400, json={"error": {"message": "Invalid token", "code": 190}})
    async with make_client(handler) as c:
        with pytest.raises(PublishError, match="Invalid token"):
            await InstagramPublisher("tok", "IG1", public_url=fake_public, client=c).publish("x", [b"1"])


# ---------- YouTube ----------

def yt_handler(calls, token_response=None):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.host == "oauth2.googleapis.com":
            return token_response or httpx.Response(200, json={"access_token": "AT"})
        if req.method == "POST" and req.url.path == "/upload/youtube/v3/videos":
            return httpx.Response(200, headers={"Location": "https://upload.youtube.example/session1"})
        if req.method == "PUT" and req.url.host == "upload.youtube.example":
            return httpx.Response(200, json={"id": "abc"})
        raise AssertionError(str(req.url))
    return handler


async def test_youtube_full_flow(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"VIDEOBYTES")
    calls = []
    text = "<b>Заголовок шортса</b><br>описание #маркетинг и #SMM, ещё #a-b"
    async with make_client(yt_handler(calls)) as c:
        res = await YouTubePublisher("cid", "sec", "rt", client=c).publish(text, video_path=str(video))
    assert res.url == "https://www.youtube.com/shorts/abc" and res.external_id == "abc" and res.platform == "youtube"
    token, init, put = calls
    assert form(token) == {"client_id": "cid", "client_secret": "sec", "refresh_token": "rt", "grant_type": "refresh_token"}
    assert init.url.params["uploadType"] == "resumable" and init.url.params["part"] == "snippet,status"
    assert init.headers["Authorization"] == "Bearer AT"
    assert init.headers["X-Upload-Content-Type"] == "video/mp4" and init.headers["X-Upload-Content-Length"] == "10"
    meta = json.loads(init.content)
    assert meta["snippet"]["title"] == "Заголовок шортса"
    assert meta["snippet"]["tags"] == ["маркетинг", "SMM", "a"]
    assert "описание" in meta["snippet"]["description"]
    assert meta["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    assert put.content == b"VIDEOBYTES" and put.headers["Content-Type"] == "video/mp4"


async def test_youtube_title_fallback_and_limit(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    calls = []
    async with make_client(yt_handler(calls)) as c:
        await YouTubePublisher("cid", "sec", "rt", client=c).publish("", video_path=str(video))
        await YouTubePublisher("cid", "sec", "rt", client=c).publish("я" * 300, video_path=str(video))
    assert json.loads(calls[1].content)["snippet"]["title"] == "Shorts"
    assert len(json.loads(calls[4].content)["snippet"]["title"]) == 100


async def test_youtube_needs_video():
    with pytest.raises(PublishError, match="только видео"):
        await YouTubePublisher("cid", "sec", "rt").publish("текст", [b"img"])


async def test_youtube_token_error(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    bad = httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token expired"})
    async with make_client(yt_handler([], bad)) as c:
        with pytest.raises(PublishError, match="invalid_grant"):
            await YouTubePublisher("cid", "sec", "rt", client=c).publish("x", video_path=str(video))


async def test_youtube_upload_error(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "AT"})
        return httpx.Response(403, json={"error": {"message": "quotaExceeded"}})
    async with make_client(handler) as c:
        with pytest.raises(PublishError, match="403.*quotaExceeded"):
            await YouTubePublisher("cid", "sec", "rt", client=c).publish("x", video_path=str(video))


def test_build_publishers_instagram_youtube():
    ps = build_publishers(threads_token="t", threads_user_id="1", ig_token="t", ig_user_id="2",
                          yt_client_id="a", yt_client_secret="b", yt_refresh_token="c")
    assert [p.platform for p in ps] == ["threads", "instagram", "youtube"]
    assert [p.platform for p in build_publishers(ig_token="t")] == []
    assert [p.platform for p in build_publishers(yt_client_id="a", yt_client_secret="b")] == []
