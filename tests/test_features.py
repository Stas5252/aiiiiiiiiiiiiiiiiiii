"""Продукт — поиск лидов и ведение Threads и Instagram: остальное выключено по умолчанию и убрано из меню,
но включается одним переключателем."""
import re
from pathlib import Path

import pytest

from leadhunter import features
from leadhunter.bot.content import CONTENT_HELP
from leadhunter.bot.handlers import HELP
from leadhunter.content.generator import ContentGenerator
from leadhunter.content.store import ContentStore
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
