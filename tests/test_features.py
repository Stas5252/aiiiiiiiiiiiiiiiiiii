"""Продукт — поиск лидов и ведение Threads и Instagram: остальное выключено по умолчанию и убрано из меню,
но включается одним переключателем."""
import re
from pathlib import Path

import pytest

from leadhunter import features
from leadhunter.bot.content import CONTENT_HELP
from leadhunter.bot.handlers import HELP
from leadhunter.content.generator import ContentGenerator
from leadhunter.content.store import ContentStore, media_json
from leadhunter.db import Database
from leadhunter.miniapp import server as miniapp_server

pytestmark = pytest.mark.product_defaults
STATIC = Path(miniapp_server.__file__).parent / "static"


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.connect()
    yield d
    await d.close()


def test_every_cabinet_switch_has_a_default():
    assert set(miniapp_server.FLAGS) <= set(features.DEFAULTS)
    assert all(features.DEFAULTS[key] == "0" for key in features.EXTRA)


async def test_core_is_on_and_extra_is_off_until_switched(db):
    for key in ("format_post", "format_carousel", "format_reel", "format_ideas",
                "publish_threads", "publish_instagram", "publish_telegram", "leads_boards", "leads_social", "leads_maps"):
        assert await features.enabled(db, key), key
    for key in features.EXTRA:
        assert not await features.enabled(db, key), key
        await db.set_setting(key, "1")
        assert await features.enabled(db, key), key


async def test_generator_refuses_switched_off_format_and_keeps_it_out_of_ideas(db):
    class LLM:
        user = ""

        async def complete_json(self, system, user, temperature=0.8):
            self.user = user
            return {"ideas": [{"topic": "Сколько стоит сайт", "kind": "toon"}, {"topic": "Чек-лист запуска", "kind": "magnet"},
                              {"topic": "Три ошибки на главной", "kind": "carousel"}]}

    llm = LLM()
    store = ContentStore(db)
    await store.init()
    gen = ContentGenerator(llm, store, db, "Студия: сайты и боты")
    with pytest.raises(ValueError, match="выключен"):
        await gen.generate("toon", "тема")
    ideas = (await gen.generate("ideas", ""))["ideas"]
    assert [i["kind"] for i in ideas] == ["post", "post", "carousel"]
    assert "toon" not in llm.user and "magnet" not in llm.user

    await db.set_setting("format_magnet", "1")
    ideas = (await gen.generate("ideas", ""))["ideas"]
    assert [i["kind"] for i in ideas] == ["post", "magnet", "carousel"]
    assert "magnet" in llm.user and "toon" not in llm.user


def test_bot_help_does_not_offer_switched_off_functions():
    for text in (HELP, CONTENT_HELP.replace(features.EXTRA_HINT, "")):
        assert not re.search(r"/toon|/aivideo|/magnet|vk|youtube", text, re.I), text
    assert features.EXTRA_HINT in CONTENT_HELP


def test_cabinet_keeps_extra_switches_under_additional():
    page = (STATIC / "index.html").read_text()
    main, extra = page.split('<details class="card" id="card-extra">')
    extra = extra.split("</details>")[0]
    for key in features.EXTRA:
        assert f'data-feature="{key}"' in extra and f'data-feature="{key}"' not in main, key
    for key in ("format_post", "format_carousel", "publish_threads", "publish_instagram"):
        assert f'data-feature="{key}"' in main, key


async def test_cabinet_api_reports_defaults_and_hides_switched_off_formats(tmp_path):
    from test_miniapp_hardening import auth, make_world

    w = await make_world(tmp_path)
    try:
        s = await (await w.client.get("/api/settings", headers=auth())).json()
        assert [s[key] for key in features.EXTRA] == [False] * len(features.EXTRA)
        assert s["format_post"] and s["publish_threads"] and s["publish_instagram"] and s["leads_boards"]
        assert not s["leads_jobs"] and not s["leads_web"]
        ch = await (await w.client.get("/api/content/channels", headers=auth())).json()
        assert ch["formats"] == ["post", "carousel", "reel"]
        r = await w.client.post("/api/content", headers=auth(), json={"kind": "magnet", "topic": "Чек-лист | пункт | пункт"})
        assert r.status == 400 and "выключен" in (await r.json())["error"]

        r = await w.client.post("/api/settings", headers=auth(), json={"format_magnet": True})
        assert r.status == 200 and (await r.json())["format_magnet"] is True
        ch = await (await w.client.get("/api/content/channels", headers=auth())).json()
        assert ch["formats"] == ["post", "carousel", "reel", "magnet"]
    finally:
        await w.client.close()
        await w.db.close()


async def test_idea_in_switched_off_format_becomes_first_enabled_format(db):
    class LLM:
        async def complete_json(self, system, user, temperature=0.8):
            return {"ideas": [{"topic": "Чек-лист запуска", "kind": "magnet"}]}

    store = ContentStore(db)
    await store.init()
    gen = ContentGenerator(LLM(), store, db, "Студия: сайты и боты")
    await db.set_setting("format_post", "0")
    assert await gen.idea_kinds() == ["carousel", "reel"]
    assert (await gen.generate("ideas", ""))["ideas"][0]["kind"] == "carousel"


async def test_autopilot_does_not_fall_back_to_switched_off_post(tmp_path):
    import json
    from datetime import datetime, timezone

    from leadhunter.content.scheduler import autopilot_once
    from test_miniapp_hardening import make_world

    w = await make_world(tmp_path)
    try:
        await w.db.set_setting("autopilot", "1")
        await w.db.set_setting("format_post", "0")
        await w.factory.store.add("idea", "Пять ошибок | первая | вторая", body=json.dumps({"topic": "Пять ошибок"}),
                                  platforms="magnet", scheduled_at=datetime.now(timezone.utc).isoformat())
        made = []

        async def deliver(row):
            made.append(row["kind"])

        assert await autopilot_once(w.factory, deliver, "UTC", 0) is not None
        assert made == ["carousel"]
    finally:
        await w.client.close()
        await w.db.close()


async def test_rebuild_of_old_ai_video_draft_sends_no_paid_requests(tmp_path):
    import json

    from test_miniapp_hardening import make_world

    class VideoAI:
        video_enabled, calls = True, 0

        async def video(self, prompt, aspect="9:16"):
            self.calls += 1
            raise AssertionError("платный запрос при выключенном ИИ-видео")

    w = await make_world(tmp_path)
    try:
        w.factory.video_ai = VideoAI()
        item_id = await w.factory.store.add("aivideo", "тема", body=json.dumps({"title": "т", "clips": [{"prompt": "кот", "line": "мяу"}]}))
        name = w.factory.media.save(b"mp4", ".mp4")
        await w.factory.store.update(item_id, media={"video": name})
        media = await w.factory.render(item_id)
        assert "выключено" in media["error"] and w.factory.video_ai.calls == 0
        # готовое видео черновика остаётся на месте — его по-прежнему можно опубликовать
        assert media_json(await w.factory.store.get(item_id)) == {"video": name}
        assert w.factory.files(await w.factory.store.get(item_id))[1]
    finally:
        await w.client.close()
        await w.db.close()
