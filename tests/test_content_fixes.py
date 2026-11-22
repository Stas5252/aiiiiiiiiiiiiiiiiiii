"""Регрессии по аудиту контент-фабрики (docs/audit/03_content_install.md, 02_bot_miniapp.md).

Номер находки — в имени теста. Каждый тест падал до правки: всё на подменённой сессии Telegram,
поддельных площадках и временной базе, без сети и ключей."""
import asyncio
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from leadhunter.bot.content import format_card
from leadhunter.content.factory import brand_from_setting, compose_text
from leadhunter.content.media import MediaStore
from leadhunter.content.publish import PublishResult, TelegramPublisher, ThreadsPublisher
from leadhunter.content.render import Brand, render_cover, render_slide
from leadhunter.content.scheduler import autopilot_once, publish_due
from leadhunter.content.store import ContentStore
from leadhunter.content.toon import normalize_script
from leadhunter.miniapp.server import MiniAppAPI, build_app
from test_content import CAROUSEL, IDEAS, MAGNET, POST, REEL, TOKEN, FakeLLM, headers, make_env
from test_content_publish import FakeSession as ChannelSession
from test_content_publish import make_client, threads_handler

TZ = "Europe/Moscow"


@pytest.fixture
async def env(tmp_path):
    e = await make_env(tmp_path, FakeLLM())
    yield e
    await e.db.close()


@pytest.fixture
async def env_noai(tmp_path):
    e = await make_env(tmp_path, None)
    yield e
    await e.db.close()


async def cabinet(env):
    return TestClient(TestServer(build_app(MiniAppAPI(env.db, TOKEN, env.deps.guard, env.deps, []))))


class Pub:
    """Поддельная площадка: считает вызовы; fail — текст ошибки или исключение."""

    def __init__(self, platform="telegram", fail=None, delay=0.0):
        self.platform, self.label, self.fail, self.delay = platform, platform.upper(), fail, delay
        self.calls = []

    async def publish(self, text, images=(), video_path=None):
        self.calls.append((text, len(images), video_path))
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.fail, BaseException):
            raise self.fail
        if self.fail:
            raise RuntimeError(self.fail)
        return PublishResult(self.platform, f"https://{self.platform}.example/{len(self.calls)}")


class ScriptedLLM:
    """Отдаёт заранее заданные ответы по очереди; исключение в очереди — бросает."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    async def complete_json(self, system, user, **kw):
        self.calls += 1
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, BaseException):
            raise a
        return json.loads(json.dumps(a))


async def overdue(env, item_id, minutes=1):
    await env.factory.store.update(item_id, status="scheduled",
                                   scheduled_at=(datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat())


def said(env) -> str:
    """Последнее, что бот написал или во что переправил своё сообщение («пишу…» → «Не получилось: …»)."""
    return [d.get("text", "") for n, d in env.session.calls if n in ("SendMessage", "EditMessageText")][-1]


def published(row):
    return [p["platform"] for p in json.loads(row["published"] or "[]")]


async def notes_sink():
    notes = []

    async def notify(t):
        notes.append(t)
    return notes, notify


# ---------- № 3, V12: публикация без дублей ----------

async def test_3_v12_double_tap_publishes_once(env):
    await env.text("/post тема")
    await env.text("/channel_set telegram @mychannel")
    sent = 0
    for _ in range(3):
        calls = await env.tap("cp:1")
        sent += sum(1 for n, _ in calls if n == "SendPhoto")
    assert sent == 1
    assert "Уже опубликовано" in env.answers(calls)[0]
    row = await env.factory.store.get(1)
    assert row["status"] == "published" and published(row) == ["telegram"]


async def test_3_partial_success_retries_only_failed_platform(env):
    await env.text("/post тема")
    tg, vk = Pub("telegram"), Pub("vk", fail="токен протух")
    out = await env.factory.publish(await env.factory.store.get(1), [tg, vk])
    row = await env.factory.store.get(1)
    assert [r.platform for r in out.results] == ["telegram"] and "токен протух" in out.errors[0]
    assert row["status"] == "published" and json.loads(row["publish_errors"])[0]["platform"] == "vk"
    card = format_card(row)
    assert "не ушло в VK" in card and "токен протух" in card
    # на карточке — «Доотправить»; повтор идёт только в VK
    await env.text("/show 1")
    assert "🔁 Доотправить" in json.dumps([d for n, d in env.session.calls if n == "SendMessage"], ensure_ascii=False, default=str)
    vk.fail = None
    out = await env.factory.publish(row, [tg, vk])
    row = await env.factory.store.get(1)
    assert (len(tg.calls), len(vk.calls)) == (1, 2) and [r.platform for r in out.results] == ["vk"]
    assert published(row) == ["telegram", "vk"] and not row["publish_errors"]
    # всё вышло — третий раз не уходит никуда
    out = await env.factory.publish(row, [tg, vk])
    assert not out.results and "уже опубликован" in out.errors[0] and (len(tg.calls), len(vk.calls)) == (1, 2)


async def test_3_two_scheduler_passes_at_once_publish_once(env):
    notes, notify = await notes_sink()
    await env.text("/post тема")
    await overdue(env, 1)
    pub = Pub(delay=0.05)
    done = await asyncio.gather(publish_due(env.factory, [pub], notify), publish_due(env.factory, [pub], notify))
    assert len(pub.calls) == 1 and sorted(done) == [0, 1] and len(notes) == 1


async def test_3_restart_mid_publish_does_not_resend(env):
    """Бот убит, когда Telegram уже принял пост, а вторая площадка ещё грузилась."""
    notes, notify = await notes_sink()
    await env.text("/post тема")
    await overdue(env, 1)
    tg = Pub("telegram")
    with pytest.raises(asyncio.CancelledError):
        await env.factory.publish(await env.factory.store.get(1), [tg, Pub("youtube", fail=asyncio.CancelledError())])
    row = await env.factory.store.get(1)
    assert row["status"] == "published" and published(row) == ["telegram"]
    assert await publish_due(env.factory, [tg, Pub("youtube")], notify) == 0 and len(tg.calls) == 1
    # доотправка после перезапуска: только туда, куда не ушло
    yt = Pub("youtube")
    await env.factory.publish(row, [tg, yt])
    assert len(tg.calls) == 1 and len(yt.calls) == 1 and published(await env.factory.store.get(1)) == ["telegram", "youtube"]


async def test_3_stuck_publishing_is_released_after_crash(env):
    """Процесс умер жёстко: запись осталась «публикуется». После перезапуска она не висит вечно и не уходит дважды."""
    store = env.factory.store
    await env.text("/post первый")
    await env.text("/post второй")
    for i in (1, 2):
        await overdue(env, i)
        assert await store.claim(i)
        assert not await store.claim(i)  # второй захват не проходит
    await store.add_published(1, {"platform": "telegram", "url": "https://t.me/c/1"})
    await store.init()
    assert [(await store.get(i))["status"] for i in (1, 2)] == ["publishing", "publishing"]  # свежие — не трогаем
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await env.db.conn.execute("UPDATE content_items SET updated_at = ?", (old,))
    await ContentStore(env.db).init()
    one, two = await store.get(1), await store.get(2)
    assert one["status"] == "published" and two["status"] == "draft" and two["scheduled_at"] is None
    tg = Pub("telegram")
    notes, notify = await notes_sink()
    assert await publish_due(env.factory, [tg], notify) == 0 and not tg.calls


# ---------- V11, № 27: расписание ----------

async def test_v11_27_schedule_only_drafts_and_only_future(env):
    store = env.factory.store
    await env.text("/post тема")
    await env.factory.publish(await store.get(1), [Pub()])
    texts = await env.text("/schedule 1 2030-01-15 10:00")
    assert "уже опубликован" in texts[0] and (await store.get(1))["status"] == "published"
    await env.text("/post вторая")
    await env.tap("cx:2")
    texts = await env.text("/schedule 2 2030-01-15 10:00")
    assert "удалён" in texts[0] and (await store.get(2))["status"] == "rejected"
    await env.text("/post третья")
    texts = await env.text("/schedule 3 2020-01-01 10:00")
    assert "уже прошло" in texts[0] and (await store.get(3))["status"] == "draft"
    assert not await store.schedule(3, datetime.now(timezone.utc) - timedelta(minutes=1))
    assert await store.schedule(3, datetime.now(timezone.utc) + timedelta(hours=1))
    # кабинет ходит в то же хранилище: опубликованное и прошлое время в расписание не попадают
    async with await cabinet(env) as c:
        r = await c.post("/api/content/1", json={"action": "schedule", "at": "2030-01-15T10:00:00"}, headers=headers())
        assert (await r.json())["item"]["status"] == "published"
        r = await c.post("/api/content/3", json={"action": "draft"}, headers=headers())
        r = await c.post("/api/content/3", json={"action": "schedule", "at": "2020-01-01T10:00:00"}, headers=headers())
        assert (await r.json())["item"]["status"] == "draft"
    pub = Pub()
    notes, notify = await notes_sink()
    assert await publish_due(env.factory, [pub], notify) == 0 and not pub.calls


# ---------- № 4: план и автопилот ----------

async def test_4_plan_ideas_go_to_autopilot_not_to_publisher(env):
    await env.text("/plan")
    ideas = await env.factory.store.recent(10, kind="idea")
    assert ideas and {i["status"] for i in ideas} == {"draft"} and all(i["scheduled_at"] for i in ideas)
    first = ideas[-1]
    await env.factory.store.update(first["id"], scheduled_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    pub = Pub()
    notes, notify = await notes_sink()
    assert await publish_due(env.factory, [pub], notify) == 0 and not pub.calls and not notes
    await env.db.set_setting("autopilot", "1")
    asked = len(env.llm.systems)
    delivered = []

    async def deliver(row):
        delivered.append(row["id"])

    made = await autopilot_once(env.factory, deliver, TZ, 0)
    row = await env.factory.store.get(made)
    assert row["parent_id"] == first["id"] and row["topic"] == first["topic"] and delivered == [made]
    assert len(env.llm.systems) == asked + 1  # один запрос — на сам пост, за новой идеей к ИИ не ходили
    assert (await env.factory.store.get(first["id"]))["status"] == "published"  # идея использована


async def test_4_old_plan_ideas_with_scheduled_status_are_fixed_on_start(env):
    """Идеи, записанные прежней версией со статусом scheduled, после обновления перестают лезть в публикацию."""
    store = env.factory.store
    idea = await store.add("idea", "Тема из старого плана", platforms="post")
    await env.db.conn.execute("UPDATE content_items SET status = 'scheduled', scheduled_at = ? WHERE id = ?",
                              ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), idea))
    await env.db.conn.commit()
    assert await store.due() == []
    await store.init()
    assert (await store.get(idea))["status"] == "draft"


# ---------- № 10: Telegram, хвост длинного поста ----------

async def test_10_telegram_tail_failure_keeps_post_published(env):
    class TailFails(ChannelSession):
        async def make_request(self, bot, method, timeout=None):
            if type(method).__name__ == "SendMessage":
                self.calls.append(("SendMessage", {}))
                raise TelegramRetryAfter(method=method, message="Too Many Requests: retry after 30", retry_after=30)
            return await super().make_request(bot, method, timeout)

    session = TailFails()
    pub = TelegramPublisher(Bot("123:ABC", session=session), "@chan")
    res = await pub.publish("слово " * 400, [b"img"])
    assert session.names == ["SendPhoto", "SendMessage"] and res.url == "https://t.me/chan/11"
    assert "продолжение текста не отправилось" in res.note
    # через фабрику: материал опубликован, повтор обложку второй раз не шлёт
    await env.text("/publish " + "слово " * 400)
    session.calls.clear()
    out = await env.factory.publish(await env.factory.store.get(1), [pub])
    assert out.results and (await env.factory.store.get(1))["status"] == "published"
    await env.factory.publish(await env.factory.store.get(1), [pub])
    assert session.names.count("SendPhoto") == 1


# ---------- № 11: автопилот и ошибка ИИ ----------

async def test_11_autopilot_ai_error_tells_owner_and_retries(tmp_path):
    request = httpx.Request("POST", "https://ai.example/chat/completions")
    limit = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    llm = ScriptedLLM(limit)
    env = await make_env(tmp_path, llm)
    try:
        notes, notify = await notes_sink()
        delivered = []

        async def deliver(row):
            delivered.append(row["id"])

        await env.db.set_setting("autopilot", "1")
        now = datetime.now(ZoneInfo(TZ)).replace(hour=11)
        assert await autopilot_once(env.factory, deliver, TZ, 10, now=now, notify=notify) is None
        assert len(notes) == 1 and "Автопилот" in notes[0] and "лимит" in notes[0] and "429" not in notes[0]
        assert await env.db.get_setting("autopilot_last") != now.strftime("%Y-%m-%d")  # день не потерян
        # ИИ ожил — в тот же день черновик приходит
        llm.answers = [IDEAS, POST]
        made = await autopilot_once(env.factory, deliver, TZ, 10, now=now, notify=notify)
        assert made and delivered == [made] and len(notes) == 1
        # а если ИИ лежит весь день — три попытки, второе сообщение и стоп до завтра
        tomorrow = now + timedelta(days=1)
        llm.answers = [limit]
        for _ in range(5):
            assert await autopilot_once(env.factory, deliver, TZ, 10, now=tomorrow, notify=notify) is None
        assert llm.calls == 3 + 3 and len(notes) == 3 and "Сегодня черновика не будет" in notes[-1]
        assert await env.db.get_setting("autopilot_last") == tomorrow.strftime("%Y-%m-%d")
        # без явного notify сообщение уходит владельцу через фабрику (так зовёт main.py)
        env.session.reset()
        await autopilot_once(env.factory, deliver, TZ, 10, now=tomorrow + timedelta(days=1))
        assert any("Автопилот" in d.get("text", "") for n, d in env.session.calls if n == "SendMessage")
    finally:
        await env.db.close()


# ---------- № 12: пропущенные слоты ----------

async def test_12_missed_slots_go_out_one_per_pass(env):
    notes, notify = await notes_sink()
    for i, topic in enumerate(("первая", "вторая", "третья"), 1):
        await env.text(f"/post {topic}")
        await overdue(env, i, minutes=60 - i)
    pub = Pub()
    assert await publish_due(env.factory, [pub], notify) == 1 and len(pub.calls) == 1
    assert [(await env.factory.store.get(i))["status"] for i in (1, 2, 3)] == ["published", "scheduled", "scheduled"]
    assert await publish_due(env.factory, [pub], notify) == 1 and len(pub.calls) == 2
    assert (await env.factory.store.get(2))["status"] == "published"


# ---------- № 13, V13: цвета бренда ----------

async def test_13_v13_bad_brand_color_rejected_and_never_breaks_render(env):
    texts = await env.text("/brand Пиксель | @pixel | #fff | #red")
    assert "Не понял цвет" in texts[0] and "#fff" in texts[0] and not await env.db.get_setting("brand")
    texts = await env.text("/brand Пиксель | @pixel | #141420 | #7c5cff")
    assert "Сохранил" in texts[0] and (await env.factory.brand()).accent == "#7c5cff"
    # кабинет (или старая версия) уже записал мусор в настройку — рендер берёт запасные цвета
    await env.db.set_setting("brand", json.dumps({"name": "Студия", "primary": "#zzzzzz", "accent": "#12"}))
    brand = await env.factory.brand()
    assert (brand.name, brand.primary, brand.accent) == ("Студия", Brand.primary, Brand.accent)
    assert brand_from_setting('{"primary": 5, "accent": null}').primary == Brand.primary
    assert brand_from_setting("[1]").accent == Brand.accent
    for cmd in ("/publish Свой текст", "/carousel тема"):
        texts = await env.text(cmd)
        assert "Не получилось" not in " ".join(texts) and "🖼" in texts[-1]
    assert Image.open(io.BytesIO(render_slide("Заголовок", "Текст", 1, 3, Brand(primary="#red", accent="blue")))).size == (1080, 1350)


# ---------- № 14, 24, 25: типы в ответе ИИ ----------

async def test_14_24_ai_answer_with_wrong_types_is_normalized(tmp_path):
    llm = ScriptedLLM({"title": None, "body": ["Первый абзац.", "Второй абзац."], "hashtags": "сайт, бот, #бизнес",
                       "image_prompt": None})
    env = await make_env(tmp_path, llm)
    try:
        texts = await env.text("/post тема")
        assert "Первый абзац.\n\nВторой абзац." in texts[-1] and "#сайт #бот #бизнес" in texts[-1] and "None" not in texts[-1]
        llm.answers = [{"title": "Карусель", "slides": [{"title": None, "text": "Текст первого"}, "Просто строка",
                                                         {"title": "Третий", "text": None}, None],
                        "caption": None, "hashtags": [None, 5, "тег"]}]
        texts = await env.text("/carousel тема")
        assert "None" not in texts[-1] and "#тег" in texts[-1] and "#5" not in texts[-1] and "3 слайдов" in texts[-1]
        slides = json.loads((await env.factory.store.get(2))["body"])["slides"]
        assert slides == [{"title": "", "text": "Текст первого"}, {"title": "", "text": "Просто строка"},
                          {"title": "Третий", "text": ""}]
        # реплика мультика строкой: карточка приходит и сразу, и по /show
        llm.answers = [{"title": "Мульт", "characters": {"boss": {"name": "Директор"}},
                        "scenes": [{"characters": [{"id": "boss", "pos": "left"}], "line": "Где заявки?"}], "caption": "x"}]
        texts = await env.text("/toon тема")
        assert "Где заявки?" in texts[-1] and "Директор" in texts[-1]
        assert "Где заявки?" in (await env.text("/show 3"))[-1]
    finally:
        await env.db.close()


async def test_25_empty_ai_answer_makes_no_draft(tmp_path):
    llm = ScriptedLLM({})
    env = await make_env(tmp_path, llm)
    try:
        for cmd, answer, why in (("/post тема", {}, "не по формату"),
                                 ("/post тема", {"title": "Заголовок", "body": None}, "нет текста"),
                                 ("/carousel тема", {"title": "Без слайдов"}, "ни одного слайда"),
                                 ("/carousel тема", {"title": "Слайды строкой", "slides": "раз, два"}, "ни одного слайда"),
                                 ("/reel тема", {"title": "Без сцен", "scenes": []}, "ни одной сцены"),
                                 ("/magnet тема", {"title": "Без пунктов"}, "ни одного пункта")):
            llm.answers = [answer]
            await env.text(cmd)
            assert "Не получилось" in said(env) and why in said(env), (cmd, said(env))
        assert await env.factory.store.recent(10) == []
    finally:
        await env.db.close()


# ---------- № 22, 23: тексты ошибок ----------

async def test_23_ai_errors_are_plain_russian_and_bad_json_is_retried(tmp_path):
    bad_json = json.JSONDecodeError("Expecting value", "", 0)
    llm = ScriptedLLM(bad_json, POST)
    env = await make_env(tmp_path, llm)
    try:
        texts = await env.text("/post тема")  # первый ответ не JSON — повторили, пост готов
        assert llm.calls == 2 and "Сколько стоит сайт" in texts[-1]
        request = httpx.Request("POST", "https://ai.example/chat/completions")
        for error, text in ((bad_json, "не по формату"),
                            (AttributeError("'NoneType' object has no attribute 'strip'"), "не по формату"),
                            (IndexError("list index out of range"), "не по формату"),
                            (httpx.ReadTimeout(""), "не ответил вовремя"),
                            (asyncio.TimeoutError(), "не ответил вовремя"),
                            (httpx.ConnectError("[Errno -2] Name or service not known"), "Нет связи с ИИ"),
                            (httpx.HTTPStatusError("x", request=request, response=httpx.Response(401, request=request)), "ключ"),
                            (RuntimeError("лимит 300 вызовов ИИ в час исчерпан"), "300 вызовов")):
            llm.answers = [error]
            await env.text("/post тема")
            answer = said(env)
            assert answer.startswith("Не получилось: ") and text in answer, (error, answer)
            assert not re.search(r"Errno|NoneType|out of range|Expecting value|Error\b", answer), answer
        llm.answers = [[POST]]  # JSON-массив из одного объекта — разбираем
        assert "Сколько стоит сайт" in (await env.text("/post тема"))[-1]
    finally:
        await env.db.close()


async def test_22_timeout_gives_readable_publish_error(env):
    await env.text("/post тема")
    out = await env.factory.publish(await env.factory.store.get(1), [Pub("youtube", fail=asyncio.TimeoutError())])
    assert out.errors == ["YOUTUBE: не дождался ответа (таймаут)"]
    out = await env.factory.publish(await env.factory.store.get(1), [Pub("vk", fail=KeyError())])
    assert out.errors == ["VK: KeyError"]


# ---------- № 15: правка лид-магнита ----------

async def test_15_magnet_edit_reaches_the_channel(env):
    await env.text("/magnet чек-лист")
    row = await env.factory.store.get(1)
    assert compose_text(row).startswith(MAGNET["post"])
    calls = await env.tap("ce:1")
    assert any("текст анонса" in d.get("text", "") for n, d in calls if n == "SendMessage")
    texts = await env.text("МОЙ НОВЫЙ АНОНС от владельца")
    assert "МОЙ НОВЫЙ АНОНС" in texts[-1] and MAGNET["post"] not in texts[-1]
    pub = Pub()
    await env.factory.publish(await env.factory.store.get(1), [pub])
    assert pub.calls[0][0].startswith("МОЙ НОВЫЙ АНОНС от владельца") and "#чеклист" in pub.calls[0][0]
    assert await env.factory.store.edits("magnet") == [(MAGNET["post"], "МОЙ НОВЫЙ АНОНС от владельца")]
    # то же из кабинета: правка подписи — это правка анонса
    await env.text("/magnet второй")
    async with await cabinet(env) as c:
        await c.post("/api/content/2", json={"action": "edit", "caption": "Анонс из кабинета"}, headers=headers())
    assert compose_text(await env.factory.store.get(2)).startswith("Анонс из кабинета")


# ---------- № 16: упавшая сборка видео ----------

async def test_16_failed_video_leaves_no_file_and_is_not_published(env, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("ffmpeg не найден")

    monkeypatch.setattr("leadhunter.content.video.build_video", broken)
    texts = await env.text("/reel тема")
    assert "видео не собрано: ffmpeg не найден" in texts[-1]
    root = env.factory.media.root
    assert [f for f in os.listdir(root) if f.endswith(".mp4")] == []
    pub = Pub()
    out = await env.factory.publish(await env.factory.store.get(1), [pub])
    row = await env.factory.store.get(1)
    assert not pub.calls and not out.results and "видео не собрано" in out.errors[0] and row["status"] == "draft"
    # по расписанию такой ролик тоже не уходит голым текстом
    notes, notify = await notes_sink()
    await overdue(env, 1)
    await publish_due(env.factory, [pub], notify)
    assert not pub.calls and "видео не собрано" in notes[0] and (await env.factory.store.get(1))["status"] == "draft"
    # пустой файл от прежней версии — тоже не видео
    name = env.factory.media.save(b"", ".mp4")
    await env.factory.store.update(1, media={"video": name})
    assert env.factory.files(await env.factory.store.get(1))[1] is None
    # кнопка «Пересобрать» чинит ролик, когда сборка заработала
    async def works(scenes, brand, path, **k):
        with open(path, "wb") as f:
            f.write(b"mp4")

    monkeypatch.setattr("leadhunter.content.video.build_video", works)
    calls = await env.tap("cm:1")
    assert any(n == "SendVideo" for n, _ in calls)
    await env.factory.publish(await env.factory.store.get(1), [pub])
    assert len(pub.calls) == 1 and pub.calls[0][2] and (await env.factory.store.get(1))["status"] == "published"


# ---------- № 17: лимит озвучки ----------

async def test_17_tts_limit_stops_voice_for_the_whole_build(env, monkeypatch):
    class LimitedTTS:
        images_enabled, tts_enabled = False, True

        def __init__(self):
            self.calls = 0

        async def speech(self, text):
            self.calls += 1
            if self.calls > 2:
                raise RuntimeError("Gemini 429: Rate limit exceeded (limit: 3 requests per minute)")
            return b"RIFF" + b"\x00" * 40

    built = []

    async def fake_build(scenes, brand, path, **k):
        built.append([bool(s.get("audio")) for s in scenes])
        with open(path, "wb") as f:
            f.write(b"mp4")

    monkeypatch.setattr("leadhunter.content.video.build_video", fake_build)
    env.factory.ai = tts = LimitedTTS()
    scenes = [{"text": f"Сцена {i}", "voice": f"Озвучка {i}", "duration": 2} for i in range(8)]
    row = await env.factory.create("reel", "тема", data={**REEL, "scenes": scenes})
    assert tts.calls == 3  # после первого отказа ИИ больше не дёргаем (было: по запросу с ожиданием на каждую сцену)
    assert built == [[False] * 8]  # наполовину озвученный ролик не собираем
    card = format_card(row)
    assert "без озвучки" in card and "429" in card and "♻️ Пересобрать" in str(await _keyboard(env, row))


async def _keyboard(env, row):
    from leadhunter.bot.content import keyboard
    return [b.text for line in keyboard(row).inline_keyboard for b in line]


async def test_17_gemini_tts_minute_limit_waits_once(monkeypatch):
    from leadhunter.content import gemini as g

    sleeps, calls = [], []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(g, "_sleep", fake_sleep)

    def handler(request: httpx.Request):
        calls.append(1)
        return httpx.Response(429, json={"error": {"message": "Rate limit exceeded (limit: 3 requests per minute). Please retry in 58s"}})

    media = g.GeminiMedia("AIzaTest", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(g.GeminiError, match="429"):
        await media.speech("Привет")
    assert sleeps == [60.0] and len(calls) == 2


# ---------- № 18: эмодзи ----------

def test_18_emoji_are_not_drawn_as_empty_boxes():
    brand, plain_brand = Brand(name="Студия 🚀", handle="@studio"), Brand(name="Студия", handle="@studio")
    assert render_slide("Запуск бота", "", 2, 5, brand) == render_slide("Запуск бота", "", 2, 5, plain_brand)
    assert render_slide("Запуск 🚀 бота 🔥", "Клиенты 💅 довольны 👍", 2, 5, brand) == \
        render_slide("Запуск бота", "Клиенты довольны", 2, 5, brand)
    assert render_cover("Привет ✅", "", brand) == render_cover("Привет", "", brand)
    from leadhunter.content.render import render_scene
    assert render_scene("Бот окупается 🚀", brand, subtitle="за неделю 🔥") == render_scene("Бот окупается", brand, subtitle="за неделю")
    from leadhunter.content.toon import render_frame
    script = {"characters": {"a": {"name": "Аня 💅"}}, "scenes": [
        {"bg": "office", "caption": "Утро 🔥", "characters": [{"id": "a", "pos": "left"}], "line": {"speaker": "a", "text": "Привет 🚀"}}]}
    plain = json.loads(json.dumps(script, ensure_ascii=False).replace(" 💅", "").replace(" 🔥", "").replace(" 🚀", ""))
    assert render_frame(script, 0, 1.0, brand).tobytes() == render_frame(plain, 0, 1.0, plain_brand).tobytes()
    # знаки, которые в шрифте есть, остаются
    assert render_slide("Цена → 30 000 ₽", "№ 1 — «да»", 2, 5, brand) != render_slide("Цена 30 000", "1 да", 2, 5, brand)


# ---------- № 19, 21: файлы для Threads / Instagram ----------

async def test_19_21_public_copy_is_real_jpeg_and_removed_after_publish(env, tmp_path):
    media = MediaStore(str(tmp_path / "m"), "https://bot.example.com/app")
    png = render_slide("Слайд", "Текст", 2, 3, Brand())
    url = await media.public_url(png, ".jpg")
    name = url.rsplit("/", 1)[1]
    assert url.startswith("https://bot.example.com/media/") and media.read(name)[:3] == b"\xff\xd8\xff"
    assert Image.open(io.BytesIO(media.read(name))).size == (1080, 1350)

    env.factory.media.public_base = "https://bot.example.com"
    await env.text("/carousel тема")
    before = sorted(os.listdir(env.factory.media.root))
    fetched, calls = [], []

    async def public_url(data, ext):
        u = await env.factory.media.public_url(data, ext)
        fetched.append(env.factory.media.read(u.rsplit("/", 1)[1])[:3])
        return u

    async with make_client(threads_handler(calls)) as c:
        threads = ThreadsPublisher("tok", "U1", public_url=public_url, client=c)
        for _ in range(2):  # и удачная публикация, и повтор не оставляют копий
            out = await env.factory.publish(await env.factory.store.get(1), [threads])
    assert out.errors and len(fetched) == len(CAROUSEL["slides"]) and set(fetched) == {b"\xff\xd8\xff"}
    assert sorted(os.listdir(env.factory.media.root)) == before
    # копии, оставшиеся от оборванной публикации, убираются при запуске; материалы владельца — нет
    stale = env.factory.media.save(b"x", ".jpg", "pub.")
    os.utime(env.factory.media.path(stale), (0, 0))
    MediaStore(env.factory.media.root)
    assert sorted(os.listdir(env.factory.media.root)) == before


# ---------- № 20: обрезка карточки ----------

def test_20_long_card_is_cut_without_breaking_html():
    class Row(dict):
        def __getitem__(self, k):
            return self.get(k)

    items = [{"title": f"Пункт {i} <важно> & \"точно\"", "text": "Что проверить перед запуском & зачем " * 6} for i in range(15)]
    for n in range(120):
        row = Row(id=1, kind="magnet", status="draft", topic="т", title="З" * n, caption="", hashtags="#a", media="",
                  body=json.dumps({"subtitle": "s", "items": items, "post": "Анонс & ещё"}, ensure_ascii=False))
        card = format_card(row)
        assert len(card) <= 4010, n
        assert card.count("<b>") == card.count("</b>"), n
        rest = re.sub(r"</?b>|&(amp|lt|gt|quot|#x27);", "", card)
        assert "<" not in rest and ">" not in rest and "&" not in rest, (n, card[-60:])


# ---------- № 26: Threads режет текст ----------

async def test_26_threads_truncation_is_reported():
    calls = []
    async with make_client(threads_handler(calls)) as c:
        res = await ThreadsPublisher("tok", "U1", client=c).publish("а" * 800)
        short = await ThreadsPublisher("tok", "U1", client=c).publish("а" * 100)
    assert len(calls[0][2]["text"]) == 500 and "обрезан до 500" in res.note and "800" in res.note and short.note == ""


# ---------- № 28: обложка поста ----------

async def test_28_cover_is_4x5_and_does_not_repeat_the_title(env_noai):
    env = env_noai
    await env.text("/publish Сайт за неделю\nДелаем быстро и без предоплаты")
    await env.text("/publish Только одна строка")
    brand = await env.factory.brand()
    covers = [env.factory.files(await env.factory.store.get(i))[0][0] for i in (1, 2)]
    assert Image.open(io.BytesIO(covers[0])).size == (1080, 1350)
    assert covers[0] == render_cover("Сайт за неделю", "Делаем быстро и без предоплаты", brand)
    assert covers[1] == render_cover("Только одна строка", "", brand)


# ---------- № 29: мультик одинаков между запусками ----------

def test_29_toon_frame_is_the_same_in_every_process():
    code = ("import hashlib; from leadhunter.content.render import Brand; from leadhunter.content.toon import render_frame; "
            "s = {'characters': {'dev': {'name': 'Разработчик'}, 'boss': {'name': 'Директор'}}, 'scenes': [{'bg': 'office', "
            "'characters': [{'id': 'dev', 'pos': 'left'}, {'id': 'boss', 'pos': 'right'}], 'line': {'speaker': 'dev', 'text': 'Готово'}}]}; "
            "print(hashlib.md5(render_frame(s, 0, 1.0, Brand(), (270, 480)).tobytes()).hexdigest())")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    digests = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=root,
                              env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip() for seed in ("1", "2", "3")}
    assert len(digests) == 1


# ---------- № 30: /toon без ИИ ----------

async def test_30_manual_toon_puts_every_speaker_in_frame(env_noai, monkeypatch):
    async def fake_build(script, brand, path, **k):
        with open(path, "wb") as f:
            f.write(b"mp4")

    monkeypatch.setattr("leadhunter.content.toon.build_toon", fake_build)
    env = env_noai
    texts = await env.text("/toon Планёрка | А: раз | Б: два | В: три | Г: четыре | Д: пять")
    assert "видео собрано" in texts[-1]
    script = normalize_script(json.loads((await env.factory.store.get(1))["body"]))
    assert [c["name"] for c in script["characters"].values()] == ["А", "Б", "В", "Г", "Д"]
    for scene in script["scenes"]:
        cast = [c["id"] for c in scene["characters"]]
        talking = [c["id"] for c in scene["characters"] if c["action"] == "talk"]
        assert scene["line"]["speaker"] in cast and talking == [scene["line"]["speaker"]] and 2 <= len(cast) <= 3
    assert [s["line"]["text"] for s in script["scenes"]] == ["раз", "два", "три", "четыре", "пять"]
    # ИИ забыл поставить говорящего в кадр — ставим сами
    script = normalize_script({"characters": {"a": {}, "b": {}}, "scenes": [
        {"characters": [{"id": "a", "pos": "left"}], "line": {"speaker": "b", "text": "я тут"}}]})
    assert [c["id"] for c in script["scenes"][0]["characters"]] == ["a", "b"]


# ---------- M11: «о чём?» не висит вечно ----------

async def test_m11_other_command_cancels_waiting_for_topic(env):
    assert "о чём?" in (await env.text("/post"))[0]
    await env.text("/leads")  # команда другого раздела бота
    await env.text("просто сообщение, не тема")
    assert await env.factory.store.recent(10) == []
    await env.text("/carousel")
    await env.text("/queue")
    await env.text("и это не тема")
    assert await env.factory.store.recent(10) == []
    # правка текста: передумал, дал другую команду — следующий текст не затирает пост
    await env.text("/post тема")
    await env.tap("ce:1")
    await env.text("/content")
    await env.text("случайный текст")
    assert (await env.factory.store.get(1))["body"] == POST["body"]
    # обычный путь работает
    await env.text("/post")
    texts = await env.text("Сколько стоит бот")
    assert "Сколько стоит сайт" in texts[-1] and len(await env.factory.store.recent(10)) == 2


# ---------- M15: подсказки без ИИ ----------

async def test_m15_no_ai_hints_send_to_setup(env_noai):
    env = env_noai
    said = [(await env.text("/post тема"))[0], (await env.text("/ideas"))[0], (await env.text("/aivideo тема"))[0]]
    await env.text("/publish Свой текст")
    said += [env.answers(await env.tap("cr:1"))[0], env.answers(await env.tap("cv:1"))[0]]
    for text in said:
        assert "/setup" in text and "OPENAI_API_KEY" not in text and ".env" not in text, text
    with pytest.raises(ValueError, match="/setup"):
        await env.factory.gen.generate("post", "тема")


# ---------- ручные форматы без ИИ из кабинета ----------

async def test_manual_formats_work_from_cabinet_without_ai(env_noai, monkeypatch):
    async def fake_build(scenes, brand, path, **k):
        with open(path, "wb") as f:
            f.write(b"mp4")

    monkeypatch.setattr("leadhunter.content.video.build_video", fake_build)
    async with await cabinet(env_noai) as c:
        r = await c.post("/api/content", json={"kind": "carousel", "topic": "Три совета | Совет раз | Совет два"}, headers=headers())
        item = (await r.json())["item"]
        assert r.status == 200 and item["title"] == "Три совета" and len(item["images"]) == 3
        r = await c.post("/api/content", json={"kind": "reel", "topic": "Фраза раз | Фраза два"}, headers=headers())
        assert r.status == 200 and (await r.json())["item"]["video"]
        r = await c.post("/api/content", json={"kind": "post", "topic": "тема"}, headers=headers())
        hint = (await r.json())["error"]
        assert r.status == 400 and "/setup" in hint and "Заголовок | слайд" in hint
