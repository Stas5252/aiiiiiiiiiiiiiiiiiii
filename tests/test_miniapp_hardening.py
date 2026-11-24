"""Кабинет нельзя сломать или обойти запросом; заявки с лендинга доходят и не превращаются в спам.
Номера в названиях тестов — находки из docs/audit/02_bot_miniapp.md."""
import asyncio
import json
import re
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from leadhunter.bot.guard import Guard
from leadhunter.bot.handlers import BotDeps
from leadhunter.bot.workspace import Workspace
from leadhunter.config import Settings
from leadhunter.content.runtime import build_content
from leadhunter.db import Database
from leadhunter.integrations import DB_PREFIX, Integrations
from leadhunter.miniapp.auth import sign_init_data, validate_init_data
from leadhunter.miniapp.server import MiniAppAPI, build_app
from leadhunter.models import Lead
from leadhunter.phrases import PhraseProvider
from leadhunter.pipeline import Pipeline

TOKEN = "123456:FAKE"
OWNER, MEMBER, MEMBER2 = 777, 200, 300
MAGNET = {"title": "Чек-лист: сайт, который продаёт", "subtitle": "10 пунктов перед запуском",
          "items": [{"title": f"Пункт {i}", "text": "Что проверить"} for i in range(1, 4)], "cta": "Оставьте контакт",
          "post": "Забирайте чек-лист", "hashtags": ["#чеклист"], "image_prompt": "x"}


PEOPLE = [("Оля", "@olya"), ("Вера", "+7 900 111-22-33"), ("Игорь", "igor@example.com"),
          ("Нина", "@nina_k"), ("Павел", "+7 912 000-11-22"), ("", "@anon_user")]


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, lead_id, lead, info):
        self.sent.append(lead_id)


class SpyPipeline(Pipeline):
    """Настоящий конвейер, который запоминает, какие заявки с лендинга ему передал кабинет."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.landing: list[Lead] = []

    async def handle(self, lead, **kw):
        if lead.source == "landing":
            self.landing.append(lead)
        return await super().handle(lead, **kw)


class World:
    """Кабинет на временной базе: настоящие конвейер и контент-фабрика, без Telegram и без ИИ."""

    def __init__(self, db, api, client, factory, notifier, deps):
        self.db, self.api, self.client, self.factory, self.notifier, self.deps = db, api, client, factory, notifier, deps

    @property
    def passed(self) -> list[str]:
        """Контакты заявок, которые кабинет передал в конвейер лидов (каждая — лид и уведомление в Telegram)."""
        return [lead.contact for lead in self.deps.extra["pipeline"].landing]

    async def magnet(self) -> str:
        item_id = await self.factory.save("magnet", "чек-лист", dict(MAGNET))
        return (await self.factory.store.get(item_id))["slug"]

    async def form_token(self, slug: str) -> str:
        page = await (await self.client.get(f"/lp/{slug}")).text()
        return page.split('name="token" value="')[1].split('"')[0] if 'name="token"' in page else ""

    async def submit(self, slug: str, contact: str, *, ip: str = "203.0.113.1", token: str | None = None, **fields):
        data = {"contact": contact, "token": await self.form_token(slug) if token is None else token, **fields}
        return await self.client.post(f"/lp/{slug}", data=data, headers={"X-Forwarded-For": ip})

    async def leads(self, source: str = "landing") -> list:
        cur = await self.db.conn.execute("SELECT * FROM leads WHERE source = ? ORDER BY id", (source,))
        return list(await cur.fetchall())

    async def landing_rows(self) -> list:
        cur = await self.db.conn.execute("SELECT * FROM landing_leads ORDER BY id")
        return list(await cur.fetchall())


async def make_world(tmp_path, dev_user_id=None, bot=None):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    notifier = FakeNotifier()
    pipeline = SpyPipeline(db, notifier, default_threshold=5)
    await pipeline.handle(Lead(source="kwork", external_id="1", title="Нужен сайт для салона",
                               text="Бюджет 40 000 руб, срочно", intent_implied=True, url="https://kwork.ru/projects/1"))
    await pipeline.handle(Lead(source="telegram", external_id="2", text="Ищу разработчика телеграм бота для записи клиентов",
                               contact="@anna", url="javascript:alert(document.domain)"))
    notifier.sent.clear()
    await db.add_member(MEMBER, "Маша")
    await db.add_member(MEMBER2, "Петя")
    guard = Guard(bot, db, [OWNER])
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), Workspace(None, db), None, "профиль")
    deps.extra["threshold"] = 5
    deps.extra["pipeline"] = pipeline
    s = Settings(bot_token=TOKEN, content_dir=str(tmp_path / "content"), _env_file=None)
    factory = await build_content(s, db, None, None, deps, None)
    deps.extra["integrations"] = Integrations(db, s, None)
    api = MiniAppAPI(db, TOKEN, guard, deps, [], dev_user_id=dev_user_id)
    client = TestClient(TestServer(build_app(api)))
    await client.start_server()
    return World(db, api, client, factory, notifier, deps)


GROUP = -1001234567890


class FakeGroupBot:
    """Вместо Telegram: отвечает, кто сейчас состоит в рабочей группе."""

    def __init__(self, statuses: dict[int, str]):
        self.statuses, self.down, self.asked, self.delay = statuses, False, [], 0.0

    async def get_chat_member(self, chat_id, user_id):
        from types import SimpleNamespace
        self.asked.append((chat_id, user_id))
        await asyncio.sleep(self.delay)  # ответ Telegram приходит не мгновенно
        if self.down:
            raise RuntimeError("Telegram недоступен")
        return SimpleNamespace(status=self.statuses.get(user_id, "left"), is_member=False)


@pytest.fixture
async def grouped(tmp_path):
    """Кабинет с рабочей группой: MEMBER и MEMBER2 в команде и пока оба состоят в группе."""
    bot = FakeGroupBot({MEMBER: "member", MEMBER2: "administrator"})
    world = await make_world(tmp_path, bot=bot)
    await world.db.set_setting("workspace_chat_id", str(GROUP))
    world.bot = bot
    yield world
    await world.client.close()
    await world.db.close()


@pytest.fixture
async def w(tmp_path):
    world = await make_world(tmp_path)
    yield world
    await world.client.close()
    await world.db.close()


@pytest.fixture
async def dev(tmp_path):
    world = await make_world(tmp_path, dev_user_id=OWNER)
    yield world
    await world.client.close()
    await world.db.close()


def auth(user_id=OWNER, name="Стас", auth_date=None):
    return {"X-Init-Data": sign_init_data({"auth_date": int(time.time()) if auth_date is None else auth_date, "query_id": "q",
                                           "user": {"id": user_id, "first_name": name}}, TOKEN)}


async def error_of(r, status=400) -> str:
    """Ошибка API — JSON с полем error, а не текст и не traceback."""
    assert r.status == status, await r.text()
    assert r.content_type == "application/json"
    return (await r.json())["error"]


# ---------- K2: форма лендинга ----------

async def test_k2_landing_limits_requests_from_one_address(w):
    slug = await w.magnet()
    statuses = [(await w.submit(slug, f"@person{i}", name=f"Человек {i}")).status for i in range(6)]
    assert statuses == [200, 200, 200, 429, 429, 429]
    assert w.passed == ["@person0", "@person1", "@person2"] and len(await w.landing_rows()) == 3
    r = await w.submit(slug, "@person9")
    assert r.status == 429 and int(r.headers["Retry-After"]) > 0 and "Слишком много" in await r.text()
    # другой посетитель по-прежнему может оставить заявку
    assert (await w.submit(slug, "@other", ip="203.0.113.77")).status == 200


async def test_k2_burst_of_200_posts_does_not_become_200_leads(w):
    slug = await w.magnet()
    tokens = [await w.form_token(slug) for _ in range(40)]

    async def post(i):
        data = {"contact": f"@spam{i}", "message": f"текст {i}", "token": tokens[i] if i < len(tokens) else ""}
        return (await w.client.post(f"/lp/{slug}", data=data, headers={"X-Forwarded-For": "198.51.100.9"})).status

    statuses = await asyncio.gather(*(post(i) for i in range(200)))
    assert statuses.count(200) == 3 and statuses.count(429) >= 170
    assert len(w.passed) == 3 and len(await w.landing_rows()) == 3 and len(w.notifier.sent) <= 3


async def test_k2_page_limit_holds_across_addresses(w):
    from leadhunter.miniapp.landing_guard import SLUG_LIMIT
    slug = await w.magnet()
    statuses = [(await w.submit(slug, f"@p{i}", ip=f"198.51.100.{i + 1}")).status for i in range(SLUG_LIMIT + 2)]
    assert statuses == [200] * SLUG_LIMIT + [429, 429]
    assert len(w.passed) == SLUG_LIMIT and len(await w.landing_rows()) == SLUG_LIMIT


async def test_k2_form_token_required_single_use_bound_to_page_and_expires(w):
    slug, other = await w.magnet(), await w.magnet()
    # без токена: заявки нет, человеку вернулась форма с его текстом и свежим токеном
    r = await w.submit(slug, "@olya", token="", name="Оля", message="хочу <сайт>")
    page = await r.text()
    assert r.status == 400 and "Страница устарела" in page
    assert 'value="@olya"' in page and 'value="Оля"' in page and "хочу &lt;сайт&gt;" in page and 'name="token" value="' in page
    assert (await w.submit(slug, "@olya", token="1.x.y")).status == 400
    assert (await w.submit(slug, "@olya", token=await w.form_token(other))).status == 400  # токен другой страницы
    assert w.passed == [] and await w.leads() == [] and await w.landing_rows() == []

    token = await w.form_token(slug)
    assert (await w.submit(slug, "@olya", token=token)).status == 200
    assert (await w.submit(slug, "@vera", token=token)).status == 400  # повторно тот же токен
    assert w.passed == ["@olya"] and [l["contact"] for l in await w.leads()] == ["@olya"]

    old = await w.form_token(slug)
    tokens = w.api.landing_guard.tokens
    tokens.now = lambda: time.time() + tokens.ttl + 5
    assert (await w.submit(slug, "@nina", token=old, ip="203.0.113.5")).status == 400  # просрочен
    assert w.passed == ["@olya"]


async def test_k2_repeat_from_same_contact_is_merged(w):
    slug = await w.magnet()
    assert (await w.submit(slug, "+7 (900) 111-22-33", name="Олег", message="нужен сайт")).status == 200
    r = await w.submit(slug, "89001112233", name="Олег", message="нужен сайт")
    assert r.status == 200 and "Спасибо" in await r.text()
    assert len(w.passed) == 1 and len(await w.leads()) == 1 and len(w.notifier.sent) == 1 and len(await w.landing_rows()) == 1
    # новый текст от того же человека не теряется, но второго лида и уведомления нет
    assert (await w.submit(slug, "8 900 111 22 33", message="и ещё бот", ip="203.0.113.2")).status == 200
    assert len(w.passed) == 1 and len(await w.leads()) == 1 and len(w.notifier.sent) == 1
    assert [r["message"] for r in await w.landing_rows()] == ["нужен сайт", "и ещё бот"]


def test_k2_client_ip_trusts_only_the_proxy_appended_address():
    from leadhunter.miniapp.landing_guard import RateLimiter, client_ip, contact_key

    class Req:
        def __init__(self, remote, **headers):
            self.remote, self.headers = remote, headers

    # прокси на этой же машине: настоящий адрес — последний в X-Forwarded-For, первые подставляет сам клиент
    assert client_ip(Req("127.0.0.1", **{"X-Forwarded-For": "1.1.1.1, 203.0.113.7"})) == "203.0.113.7"
    assert client_ip(Req("127.0.0.1", **{"X-Forwarded-For": "9.9.9.9, 203.0.113.7"})) == "203.0.113.7"
    assert client_ip(Req("127.0.0.1", **{"CF-Connecting-IP": "203.0.113.8"})) == "203.0.113.8"
    # прямое подключение из интернета: заголовкам не верим
    assert client_ip(Req("198.51.100.4", **{"X-Forwarded-For": "1.1.1.1"})) == "198.51.100.4"
    assert client_ip(Req("8.8.8.8", **{"CF-Connecting-IP": "1.1.1.1"})) == "8.8.8.8"
    assert client_ip(Req("172.18.0.1", **{"X-Forwarded-For": "203.0.113.9"})) == "203.0.113.9"  # прокси в соседнем контейнере
    # IPv6: один абонент — одна подсеть /64
    assert client_ip(Req("2001:db8:1:2:aaaa::1")) == client_ip(Req("2001:db8:1:2:bbbb::2")) == "2001:db8:1:2::"
    assert contact_key("+7 (900) 111-22-33") == contact_key("89001112233") == "9001112233"
    assert contact_key("@Anna") == contact_key("https://t.me/anna") == "anna"

    clock = [0.0]
    rl = RateLimiter(2, 60, now=lambda: clock[0])
    assert [rl.take("a"), rl.take("a"), rl.take("a"), rl.take("b")] == [True, True, False, True]
    assert rl.retry_after("a") == 61
    clock[0] = 61
    assert rl.take("a") is True


async def test_k1_cabinet_passes_every_request_to_pipeline(w):
    """Со стороны кабинета: 6 заявок от разных людей → 6 записей и 6 разных лидов, переданных в конвейер."""
    slug = await w.magnet()
    for i, (name, contact) in enumerate(PEOPLE):
        assert (await w.submit(slug, contact, name=name, message="хочу чек-лист", ip=f"203.0.113.{i + 10}")).status == 200
    assert w.passed == [c for _, c in PEOPLE] and len(await w.landing_rows()) == 6
    sent = w.deps.extra["pipeline"].landing
    assert len({lead.external_id for lead in sent}) == 6 and len({lead.full_text for lead in sent}) == 6
    assert all(lead.preset_score == 9 and lead.source == "landing" for lead in sent)


async def test_k1_six_requests_from_different_people_become_six_leads(w):
    """Сквозной: 6 заявок от разных людей с одинаковым (и пустым) текстом → 6 лидов и 6 уведомлений."""
    slug = await w.magnet()
    for i, (name, contact) in enumerate(PEOPLE):
        r = await w.submit(slug, contact, name=name, message="" if i % 2 else "хочу чек-лист", ip=f"203.0.113.{i + 10}")
        assert r.status == 200
    leads = await w.leads()
    assert [l["contact"] for l in leads] == [c for _, c in PEOPLE]
    assert len(w.notifier.sent) == 6 and len(await w.landing_rows()) == 6
    assert all(l["score"] == 9 for l in leads)


async def test_k1_six_lookalike_requests_become_six_leads(w):
    """Тёзки без текста, телефоны отличаются парой цифр — это всё равно шесть разных людей."""
    slug = await w.magnet()
    for i in range(6):
        assert (await w.submit(slug, f"+7 900 111-22-3{i}", name="Анна", ip=f"203.0.113.{i + 10}")).status == 200
    assert len(w.passed) == 6 and len(await w.landing_rows()) == 6
    assert len(await w.leads()) == 6 and len(w.notifier.sent) == 6


async def test_k1_landing_requests_are_visible_in_cabinet(w):
    slug = await w.magnet()
    await w.submit(slug, "@olya", name="Оля", message="хочу <b>сайт</b>")
    assert (await w.client.get("/api/landing-leads")).status == 401
    r = await w.client.get("/api/landing-leads", headers=auth(MEMBER))
    items = (await r.json())["items"]
    assert len(items) == 1 and items[0]["contact"] == "@olya" and items[0]["name"] == "Оля"
    assert items[0]["message"] == "хочу <b>сайт</b>" and items[0]["page"] == MAGNET["title"] and items[0]["slug"] == slug


async def test_m3_deleted_landing_does_not_accept_requests(w):
    slug = await w.magnet()
    token = await w.form_token(slug)
    row = await w.factory.store.by_slug(slug)
    await w.factory.store.update(row["id"], status="rejected")
    assert (await w.client.get(f"/lp/{slug}")).status == 404
    assert (await w.submit(slug, "@olya", token=token)).status == 404
    assert (await w.client.post("/lp/nope", data={"contact": "@olya"})).status == 404
    assert w.passed == [] and await w.landing_rows() == []


async def test_v13_landing_css_ignores_broken_brand_color(w):
    slug = await w.magnet()
    await w.db.set_setting("brand", json.dumps({"primary": "red;}body{display:none}", "accent": "#zzzzzz"}))
    page = await (await w.client.get(f"/lp/{slug}")).text()
    assert "display:none" not in page and "zzzzzz" not in page and "--bg:#050505;--ac:#FFFFFF" in page and "--on:#000" in page


# ---------- V3: права ----------

async def test_v3_only_owner_removes_members_sets_channels_and_pause(w):
    assert "владелец" in await error_of(await w.client.post("/api/settings", json={"team_remove": MEMBER2}, headers=auth(MEMBER)), 403)
    assert await w.db.is_member(MEMBER2)
    assert (await w.client.get("/api/me", headers=auth(MEMBER2))).status == 200
    await error_of(await w.client.post("/api/settings", json={"channels": {"telegram": "@evil"}}, headers=auth(MEMBER)), 403)
    assert await w.factory.store.channels() == {}
    await error_of(await w.client.post("/api/settings", json={"paused": True}, headers=auth(MEMBER)), 403)
    assert await w.db.get_setting("paused") != "1"
    # запрещённый ключ отменяет весь запрос, а не только себя
    await error_of(await w.client.post("/api/settings", json={"geo": "Казань", "team_remove": MEMBER2}, headers=auth(MEMBER)), 403)
    assert await w.db.get_setting("geo", "") == ""
    # обычные настройки участнику доступны
    r = await w.client.post("/api/settings", json={"threshold": 6, "geo": "Москва"}, headers=auth(MEMBER))
    assert r.status == 200 and (await r.json())["threshold"] == "6"
    # владельцу можно всё
    r = await w.client.post("/api/settings", headers=auth(), json={"team_remove": MEMBER2, "paused": True,
                                                                  "channels": {"telegram": "https://t.me/my_channel", "vk": "-123"}})
    s = await r.json()
    assert r.status == 200 and s["paused"] is True and [m["user_id"] for m in s["team"]] == [MEMBER]
    assert s["channels"] == {"telegram": "@my_channel", "vk": "-123"}
    assert (await w.client.get("/api/me", headers=auth(MEMBER2))).status == 403


async def test_v3_channel_target_is_validated(w):
    for bad_target in ("https://evil.example/x", "канал с пробелом", "t.me/+AbCdEf"):
        await error_of(await w.client.post("/api/settings", json={"channels": {"telegram": bad_target}}, headers=auth()))
    await error_of(await w.client.post("/api/settings", json={"channels": {"vk": "abc"}}, headers=auth()))
    await error_of(await w.client.post("/api/settings", json={"channels": {"telegram": 5}}, headers=auth()))
    assert await w.factory.store.channels() == {}


async def test_integrations_are_listed_for_owner_only(w):
    owner = await (await w.client.get("/api/settings", headers=auth())).json()
    member = await (await w.client.get("/api/settings", headers=auth(MEMBER))).json()
    assert owner["integrations"] and member["integrations"] == []
    await error_of(await w.client.post("/api/integrations", json={"key": "gemini_api_key", "value": "x"}, headers=auth(MEMBER)), 403)


# ---------- V2 в кабинете: доступ проверяет Guard модуля C ----------

async def test_v2_member_who_left_workspace_group_loses_cabinet(grouped):
    w, bot = grouped, grouped.bot
    assert (await w.client.get("/api/leads", headers=auth(MEMBER))).status == 200
    bot.statuses[MEMBER] = "left"  # вышел из группы, событие до бота не дошло
    w.api.guard.member_ttl = 0
    err = await error_of(await w.client.get("/api/leads", headers=auth(MEMBER)), 403)
    assert "команд" in err
    assert not await w.db.is_member(MEMBER)
    # всё остальное ему тоже закрыто, включая запись
    await error_of(await w.client.post("/api/leads/1/status", json={"status": "taken"}, headers=auth(MEMBER)), 403)
    await error_of(await w.client.post("/api/settings", json={"geo": "Казань"}, headers=auth(MEMBER)), 403)
    assert (await w.db.get_lead(1))["status"] == "new" and await w.db.get_setting("geo", "") == ""
    # тот, кто остался в группе, и владелец работают дальше
    assert (await w.client.get("/api/leads", headers=auth(MEMBER2))).status == 200
    r = await w.client.get("/api/settings", headers=auth())
    assert r.status == 200 and [m["user_id"] for m in (await r.json())["team"]] == [MEMBER2]


async def test_v2_cabinet_asks_telegram_once_per_ttl_and_never_for_owner(grouped):
    w, bot = grouped, grouped.bot
    for path in ("/api/me", "/api/leads", "/api/stats", "/api/settings"):
        assert (await w.client.get(path, headers=auth(MEMBER))).status == 200
    assert bot.asked == [(GROUP, MEMBER)]  # ответ Telegram запомнен, лишних запросов нет
    bot.asked.clear()
    assert (await w.client.get("/api/me", headers=auth())).status == 200
    assert bot.asked == []  # владельцу группа не нужна


async def test_v2_telegram_down_keeps_last_answer_but_never_grants(grouped):
    w, bot = grouped, grouped.bot
    assert (await w.client.get("/api/me", headers=auth(MEMBER))).status == 200
    w.api.guard.member_ttl = 0
    bot.down = True
    assert (await w.client.get("/api/me", headers=auth(MEMBER))).status == 200  # сеть моргнула — участника не выкидываем
    # про MEMBER2 Telegram ещё ни разу не ответил: не знаем — не пускаем, но и из команды не удаляем
    await error_of(await w.client.get("/api/me", headers=auth(MEMBER2)), 403)
    assert await w.db.is_member(MEMBER) and await w.db.is_member(MEMBER2)
    bot.down = False
    assert (await w.client.get("/api/me", headers=auth(MEMBER2))).status == 200


async def test_v2_without_workspace_group_team_table_decides(w):
    """Рабочей группы нет — команду ведёт владелец; посторонний не проходит, участник проходит."""
    assert (await w.client.get("/api/me", headers=auth(MEMBER))).status == 200
    assert "команд" in await error_of(await w.client.get("/api/me", headers=auth(999)), 403)


TODAY_REQUESTS = ("/api/stats?days=1", "/api/leads?status=new,hot&limit=20", "/api/leads?status=open&mine=1&limit=20", "/api/me")


async def test_v2_parallel_cabinet_requests_ask_telegram_once(grouped):
    """Экран «Сегодня» шлёт четыре запроса сразу — Telegram о членстве спрашиваем один раз на человека."""
    w, bot = grouped, grouped.bot
    bot.delay = 0.05
    rs = await asyncio.gather(*(w.client.get(p, headers=auth(MEMBER)) for p in TODAY_REQUESTS))
    assert [r.status for r in rs] == [200] * 4
    assert bot.asked == [(GROUP, MEMBER)]
    # два разных человека одновременно — по одному запросу на каждого
    bot.asked.clear()
    w.api.guard.member_ttl = 0
    rs = await asyncio.gather(*(w.client.get(p, headers=auth(uid)) for p in TODAY_REQUESTS for uid in (MEMBER, MEMBER2)))
    assert [r.status for r in rs] == [200] * 8
    assert sorted(bot.asked) == [(GROUP, MEMBER), (GROUP, MEMBER2)]
    # следующий заход после истечения кэша спрашивает заново, а не раздаёт старый ответ вечно
    bot.asked.clear()
    assert (await w.client.get("/api/me", headers=auth(MEMBER))).status == 200
    assert bot.asked == [(GROUP, MEMBER)]


async def test_v2_parallel_requests_of_left_member_ask_once_and_all_denied(grouped):
    w, bot = grouped, grouped.bot
    bot.delay = 0.05
    bot.statuses[MEMBER] = "left"
    rs = await asyncio.gather(*(w.client.get(p, headers=auth(MEMBER)) for p in TODAY_REQUESTS))
    assert [r.status for r in rs] == [403] * 4
    assert bot.asked == [(GROUP, MEMBER)] and not await w.db.is_member(MEMBER)


async def test_v2_parallel_requests_with_telegram_down_ask_once_and_never_grant(grouped):
    w, bot = grouped, grouped.bot
    bot.delay, bot.down = 0.05, True
    rs = await asyncio.gather(*(w.client.get(p, headers=auth(MEMBER)) for p in TODAY_REQUESTS))
    assert [r.status for r in rs] == [403] * 4
    assert bot.asked == [(GROUP, MEMBER)] and await w.db.is_member(MEMBER)  # не знаем — не пускаем, но и не удаляем


async def test_v2_cancelled_request_does_not_break_the_shared_check(grouped):
    """Один из параллельных запросов оборвался (человек закрыл кабинет) — остальные получают ответ."""
    w, bot = grouped, grouped.bot
    bot.delay = 0.05
    guard = w.api.guard
    first, second = asyncio.create_task(guard.member_ok(MEMBER)), asyncio.create_task(guard.member_ok(MEMBER))
    await asyncio.sleep(0.01)
    first.cancel()
    assert await second is True and bot.asked == [(GROUP, MEMBER)]
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await guard.member_ok(MEMBER) is True and bot.asked == [(GROUP, MEMBER)]  # ответ попал в кэш


# ---------- V13–V16: проверка значений ----------

async def test_v13_brand_color_must_be_hex(w):
    for color in ("#zzzzzz", "#abc", "red", "#12345", "#1234567", 5, None):
        err = await error_of(await w.client.post("/api/settings", json={"brand": {"primary": color}}, headers=auth()))
        assert "primary" in err
    assert await w.db.get_setting("brand") is None
    r = await w.client.post("/api/settings", json={"brand": {"name": "Пиксель", "primary": "#0a0B0c", "accent": ""}}, headers=auth())
    assert r.status == 200 and (await r.json())["brand"]["primary"] == "#0a0B0c"


async def test_v13_cabinet_bot_and_landing_share_one_color_rule(w, monkeypatch):
    """Правило цвета одно — `valid_color` из content/render.py: его знают бот, рендер, кабинет и лендинг."""
    from leadhunter.content import render
    from leadhunter.content.render import valid_color
    from leadhunter.miniapp import landing, validation

    for color in ("#14141F", " #7c5cff ", "#abc", "red", "#12345", "#1234567", "#zzzzzz", "14141F", "#14141F;", ""):
        r = await w.client.post("/api/settings", json={"brand": {"primary": color}}, headers=auth())
        assert (r.status == 200) == (valid_color(color) or not color.strip()), color
    # своих копий правила в кабинете нет
    assert not hasattr(validation, "COLOR_RE") and not hasattr(landing, "_COLOR") and not hasattr(landing, "_DEFAULTS")
    # поменяли правило в одном месте — поменялось везде
    monkeypatch.setattr(render, "HEX_COLOR", re.compile(r"^#[0-9a-f]{3}$"))
    assert (await w.client.post("/api/settings", json={"brand": {"primary": "#abc"}}, headers=auth())).status == 200
    await error_of(await w.client.post("/api/settings", json={"brand": {"primary": "#14141F"}}, headers=auth()))
    page = await (await w.client.get(f"/lp/{await w.magnet()}")).text()
    assert "--bg:#abc;" in page


def test_v13_cabinet_js_has_no_own_color_rule():
    import os
    from leadhunter.miniapp.server import STATIC
    js = open(os.path.join(STATIC, "app.js"), encoding="utf-8").read()
    assert "[0-9a-f]{6}" not in js


async def test_v14_hot_lead_stays_open_and_can_be_taken(w):
    r = await w.client.post("/api/leads/1/status", json={"status": "hot"}, headers=auth())
    assert r.status == 200 and (await r.json())["lead"]["status"] == "hot"
    for query in ("status=open", "status=new,hot", "status=hot", ""):
        ids = [l["id"] for l in (await (await w.client.get(f"/api/leads?{query}", headers=auth())).json())["leads"]]
        assert 1 in ids, query
    r = await w.client.post("/api/leads/1/status", json={"status": "taken"}, headers=auth(MEMBER, "Маша"))
    lead = (await r.json())["lead"]
    assert lead["status"] == "taken" and lead["assigned_to"] == MEMBER
    # «горячий» — пометка неразобранного лида: взятый так не пометить
    await error_of(await w.client.post("/api/leads/1/status", json={"status": "hot"}, headers=auth(MEMBER)), 409)
    assert (await w.db.get_lead(1))["status"] == "taken"


async def test_closed_own_lead_returns_to_work(w):
    for status in ("taken", "lost"):
        await w.client.post("/api/leads/1/status", json={"status": status}, headers=auth())
    r = await w.client.post("/api/leads/1/status", json={"status": "taken"}, headers=auth())
    assert r.status == 200 and (await r.json())["lead"]["status"] == "taken"
    ids = [l["id"] for l in (await (await w.client.get("/api/leads?status=open&mine=1", headers=auth())).json())["leads"]]
    assert ids == [1]


async def test_v15_blank_keyword_is_rejected(w):
    before = await w.db.get_keywords()
    for body in ({"kind": "negative", "word": "  "}, {"kind": "topic", "word": "\t\n"}, {"kind": "negative", "word": None},
                 {"kind": "negative", "word": ["а"]}, {"kind": "bogus", "word": "сайт"}, {"kind": "topic", "word": "квиз", "category": 5}):
        await error_of(await w.client.post("/api/keywords", json=body, headers=auth()))
    after = await w.db.get_keywords()
    assert (after.topic, after.intent, after.negative) == (before.topic, before.intent, before.negative)
    r = await w.client.post("/api/keywords", json={"kind": "negative", "word": "  Курсы "}, headers=auth())
    assert r.status == 200 and "" not in (await r.json())["keywords"]["negative"]


async def test_v16_wrong_value_types_do_not_corrupt_settings(w):
    phrases = await w.deps.phrases.get()
    bad_bodies = [{"phrases": "строка"}, {"phrases": ["а", 5]}, {"phrases": {"a": 1}}, {"profile": None}, {"profile": ["x"]},
                  {"geo": 5}, {"voice": {}}, {"paused": "false"}, {"autopilot": 1}, {"templates": ["x"]}, {"templates": {"сайт": 5}},
                  {"fresh": "x"}, {"fresh": {"board_max_age_hours": "abc"}}, {"fresh": {"max_competitors": "abc"}},
                  {"fresh": {"max_competitors": 1.5}}, {"brand": "x"}, {"brand": {"name": 5}}, {"channels": "x"},
                  {"niche": ["realtor"]}, {"threshold": "abc"}, {"threshold": None}, {"threshold": True}, {"team_remove": "abc"}]
    for body in bad_bodies:
        await error_of(await w.client.post("/api/settings", json=body, headers=auth()))
    assert await w.deps.phrases.get() == phrases
    assert await w.db.get_setting("profile") is None and await w.db.get_setting("paused") is None
    # один неверный ключ — весь запрос не применяется
    await error_of(await w.client.post("/api/settings", json={"threshold": 9, "geo": "Казань", "phrases": "строка"}, headers=auth()))
    s = await (await w.client.get("/api/settings", headers=auth())).json()
    assert s["threshold"] == "5" and s["geo"] == "" and s["profile"] == "профиль" and s["phrases"] == phrases


# ---------- V18: режим разработки и тип содержимого ----------

async def test_v18_body_must_be_json_content_type(dev, w):
    payload = json.dumps({"profile": "взломано"})
    for world in (dev, w):
        for content_type in ("text/plain", "application/x-www-form-urlencoded"):
            r = await world.client.post("/api/settings", data=payload, headers={**auth(), "Content-Type": content_type,
                                                                              "Origin": "https://evil.example"})
            assert "application/json" in await error_of(r, 415)
        assert await world.db.get_setting("profile") is None
    r = await dev.client.post("/api/leads/1/status", data=json.dumps({"status": "spam"}), headers={"Content-Type": "text/plain"})
    await error_of(r, 415)
    assert (await dev.db.get_lead(1))["status"] == "new"


async def test_v18_dev_mode_answers_only_to_local_host_name(dev):
    assert (await dev.client.get("/api/settings")).status == 200
    for host in ("evil.example", "evil.example:8080", "192.168.1.5:8080"):
        await error_of(await dev.client.get("/api/settings", headers={"Host": host}), 403)
        r = await dev.client.post("/api/settings", json={"profile": "взломано"}, headers={"Host": host})
        await error_of(r, 403)
    assert await dev.db.get_setting("profile") is None
    assert (await dev.client.get("/api/settings", headers={"Host": "localhost:8080"})).status == 200


async def test_v18_dev_server_listens_on_loopback_only(tmp_path, monkeypatch):
    from leadhunter.miniapp import dev as dev_module

    seen, opened = [], []

    class Stop(Exception):
        pass

    class FakeSite:
        def __init__(self, runner, host, port):
            seen.append((host, port))

        async def start(self):
            raise Stop

    class TrackedDatabase(Database):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            opened.append(self)

    monkeypatch.chdir(tmp_path)  # чтобы не подхватить чужой .env
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "dev.db"))
    monkeypatch.setenv("CONTENT_DIR", str(tmp_path / "content"))
    monkeypatch.setattr(dev_module.web, "TCPSite", FakeSite)
    monkeypatch.setattr(dev_module, "Database", TrackedDatabase)
    try:
        with pytest.raises(Stop):
            await dev_module.main(1, 8099)
    finally:
        for db in opened:
            await db.close()
    assert seen == [("127.0.0.1", 8099)]


# ---------- M1–M4: неверный ввод ----------

BAD_GET = ["/api/leads?limit=abc", "/api/leads?offset=x", "/api/leads?limit=1.5", "/api/leads?status=bogus",
           "/api/leads?status=new,bogus", "/api/stats?days=abc", "/api/content?limit=abc", "/api/landing-leads?limit=abc"]
POST_PATHS = ["/api/settings", "/api/integrations", "/api/rules", "/api/keywords", "/api/leads/1/status", "/api/content",
              "/api/content/1"]


async def test_m1_bad_input_is_400_json_not_500(w):
    await w.factory.save("post", "тема", {"title": "Пост", "body": "Текст"})
    for path in BAD_GET:
        assert await error_of(await w.client.get(path, headers=auth())), path
    for path in POST_PATHS:
        for raw in ("не json", "[]", '"строка"', "5", "null", ""):
            r = await w.client.post(path, data=raw, headers={**auth(), "Content-Type": "application/json"})
            assert await error_of(r), (path, raw)
    for path, body in [("/api/leads/1/status", {"status": ["new"]}), ("/api/leads/1/status", {}), ("/api/rules", {"expr": 5}),
                       ("/api/rules", {}), ("/api/content", {"kind": ["post"], "topic": "x"}), ("/api/content", {"topic": 5}),
                       ("/api/content/1", {"action": "edit", "hashtags": {"a": 1}}), ("/api/content/1", {"action": "schedule", "at": 5}),
                       ("/api/content/1", {"action": ["publish"]}), ("/api/integrations", {"key": ["x"], "value": "y"}),
                       ("/api/integrations", {"key": "gemini_api_key", "value": 5})]:
        assert await error_of(await w.client.post(path, json=body, headers=auth())), (path, body)


async def test_m1_huge_numbers_are_handled(w):
    huge = "9" * 30
    for path in (f"/api/leads/{huge}", f"/api/content/{huge}"):
        await error_of(await w.client.get(path, headers=auth()), 404)
    await error_of(await w.client.post(f"/api/leads/{huge}/status", json={"status": "taken"}, headers=auth()), 404)
    await error_of(await w.client.delete(f"/api/rules/{huge}", headers=auth()), 404)
    for path in (f"/api/stats?days={huge}", f"/api/leads?offset={huge}", f"/api/leads?limit={huge}", "/api/stats?days=-5"):
        assert (await w.client.get(path, headers=auth())).status == 200, path
    r = await w.client.post("/api/settings", json={"threshold": int(huge), "team_remove": 10**12}, headers=auth())
    assert r.status == 200 and (await r.json())["threshold"] == "10"


async def test_m1_unexpected_error_is_json_without_details(w, monkeypatch):
    async def boom(lead_id):
        raise RuntimeError("секретная подробность /home/user/data.db")

    monkeypatch.setattr(w.db, "get_lead", boom)
    err = await error_of(await w.client.get("/api/leads/1", headers=auth()), 500)
    assert "секретная" not in err and "Попробуйте" in err


async def test_m2_limit_and_offset_are_clamped(w):
    async def ids(query):
        return [l["id"] for l in (await (await w.client.get(f"/api/leads?{query}", headers=auth())).json())["leads"]]

    assert await ids("limit=-1") == [2] and await ids("limit=0") == [2] and await ids("limit=1") == [2]
    assert await ids("limit=100000") == [2, 1] and await ids("offset=-3") == [2, 1] and await ids("limit=1&offset=1") == [1]


async def test_m4_unknown_integration_key_is_rejected(w):
    for value in ("", None, "abc"):
        err = await error_of(await w.client.post("/api/integrations", json={"key": "nope", "value": value}, headers=auth()))
        assert "неизвестное" in err
    await error_of(await w.client.post("/api/integrations", json={"value": ""}, headers=auth()))
    assert await w.db.get_setting(DB_PREFIX + "nope") is None
    # известный ключ по-прежнему снимается
    r = await w.client.post("/api/integrations", json={"key": "gemini_api_key", "value": ""}, headers=auth())
    assert r.status == 200 and "integrations" in await r.json()


# ---------- M12–M15: контент ----------

async def test_m12_content_text_is_returned_raw(w):
    body = 'Тело <img src=x onerror="alert(1)"> & всё, 2 < 3'
    item_id = await w.factory.save("post", "тема", {"title": "Пост", "body": body, "hashtags": ["#a&b"]})
    item = (await (await w.client.get(f"/api/content/{item_id}", headers=auth())).json())["item"]
    assert item["text"] == body + "\n\n#a&b"
    items = (await (await w.client.get("/api/content", headers=auth())).json())["items"]
    assert items[0]["text"] == body + "\n\n#a&b"


async def test_m13_deleted_content_is_hidden_and_cannot_be_published(w):
    keep = await w.factory.save("post", "тема", {"title": "Живой", "body": "Текст"})
    gone = await w.factory.save("post", "тема", {"title": "Удалённый", "body": "Текст"})
    r = await w.client.post(f"/api/content/{gone}", json={"action": "reject"}, headers=auth())
    assert (await r.json())["item"]["status"] == "rejected"

    async def ids(query=""):
        return [i["id"] for i in (await (await w.client.get(f"/api/content?{query}", headers=auth())).json())["items"]]

    assert await ids() == [keep] and await ids("status=draft") == [keep] and await ids("status=rejected") == [gone]
    for body in ({"action": "publish"}, {"action": "schedule", "at": "2030-01-01T10:00"}):
        assert "удалён" in await error_of(await w.client.post(f"/api/content/{gone}", json=body, headers=auth()))
    assert (await w.factory.store.get(gone))["status"] == "rejected"


class Pub:
    """Площадка-заглушка: публикует или падает с заданной причиной."""

    def __init__(self, platform, label, fail=None):
        self.platform, self.label, self.fail, self.calls = platform, label, fail, 0

    async def publish(self, text, images, video):
        from leadhunter.content.publish import PublishResult
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.fail)
        return PublishResult(self.platform, f"https://example.org/{self.platform}/{self.calls}")


def use_pubs(w, *pubs):
    async def publishers():
        return list(pubs)
    w.deps.extra["publishers"] = publishers


async def published_item(w) -> int:
    item_id = await w.factory.save("post", "тема", {"title": "Пост", "body": "Текст поста"})
    use_pubs(w, Pub("telegram", "Telegram"))
    r = await w.client.post(f"/api/content/{item_id}", json={"action": "publish"}, headers=auth())
    assert (await r.json())["item"]["status"] == "published"
    return item_id


async def test_published_content_cannot_be_rejected(w):
    item_id = await published_item(w)
    err = await error_of(await w.client.post(f"/api/content/{item_id}", json={"action": "reject"}, headers=auth()), 409)
    assert "черновик" in err
    assert (await w.factory.store.get(item_id))["status"] == "published"
    items = (await (await w.client.get("/api/content", headers=auth())).json())["items"]
    assert [i["id"] for i in items] == [item_id]
    # то, что прямо сейчас публикуется, тоже не удалить
    busy = await w.factory.save("post", "тема", {"title": "Пост 2", "body": "Текст"})
    assert await w.factory.store.claim(busy)
    await error_of(await w.client.post(f"/api/content/{busy}", json={"action": "reject"}, headers=auth()), 409)
    assert (await w.factory.store.get(busy))["status"] == "publishing"
    # черновик и запланированное удаляются как раньше
    for at in (None, "2030-01-01T10:00"):
        draft = await w.factory.save("post", "тема", {"title": "Черновик", "body": "Текст"})
        if at:
            await w.client.post(f"/api/content/{draft}", json={"action": "schedule", "at": at}, headers=auth())
        r = await w.client.post(f"/api/content/{draft}", json={"action": "reject"}, headers=auth())
        assert r.status == 200 and (await r.json())["item"]["status"] == "rejected"


async def test_published_magnet_keeps_its_landing_page(w):
    slug = await w.magnet()
    item_id = (await w.factory.store.by_slug(slug))["id"]
    use_pubs(w, Pub("telegram", "Telegram"))
    await w.client.post(f"/api/content/{item_id}", json={"action": "publish"}, headers=auth())
    await error_of(await w.client.post(f"/api/content/{item_id}", json={"action": "reject"}, headers=auth()), 409)
    assert (await w.client.get(f"/lp/{slug}")).status == 200  # ссылка из вышедшего поста не ломается


async def test_published_content_cannot_be_edited(w):
    item_id = await published_item(w)
    for body in ({"action": "edit", "body": "Подменённый текст"}, {"action": "edit", "caption": "x"}, {"action": "edit", "hashtags": ["#x"]}):
        err = await error_of(await w.client.post(f"/api/content/{item_id}", json=body, headers=auth()), 409)
        assert "черновик" in err
    row = await w.factory.store.get(item_id)
    assert row["body"] == "Текст поста" and not row["caption"] and not row["hashtags"]
    assert await w.factory.store.edits("post") == []  # несостоявшаяся правка не учит ИИ
    # публикуется сейчас и удалённое — тоже не править
    busy = await w.factory.save("post", "тема", {"title": "Пост 2", "body": "Текст"})
    assert await w.factory.store.claim(busy)
    gone = await w.factory.save("post", "тема", {"title": "Пост 3", "body": "Текст"})
    await w.factory.store.reject(gone)
    for other in (busy, gone):
        await error_of(await w.client.post(f"/api/content/{other}", json={"action": "edit", "body": "x"}, headers=auth()), 409)
    # черновик правится как раньше
    draft = await w.factory.save("post", "тема", {"title": "Черновик", "body": "Было"})
    r = await w.client.post(f"/api/content/{draft}", json={"action": "edit", "body": "Стало"}, headers=auth())
    assert r.status == 200 and (await r.json())["item"]["body"] == "Стало"
    assert await w.factory.store.edits("post") == [("Было", "Стало")]


async def test_published_content_cannot_be_rescheduled_and_past_time_is_refused(w):
    item_id = await published_item(w)
    for body in ({"action": "schedule", "at": "2030-01-01T10:00"}, {"action": "draft"}):
        await error_of(await w.client.post(f"/api/content/{item_id}", json=body, headers=auth()), 409)
    assert (await w.factory.store.get(item_id))["status"] == "published"
    draft = await w.factory.save("post", "тема", {"title": "Черновик", "body": "Текст"})
    err = await error_of(await w.client.post(f"/api/content/{draft}", json={"action": "schedule", "at": "2020-01-01T10:00"}, headers=auth()))
    assert "прошл" in err and (await w.factory.store.get(draft))["status"] == "draft"


async def test_failed_platforms_are_shown_and_resent_only_there(w):
    item_id = await w.factory.save("post", "тема", {"title": "Пост", "body": "Текст поста"})
    tg, vk = Pub("telegram", "Telegram"), Pub("vk", "VK", fail="токен протух: access_token=TOK456")
    use_pubs(w, tg, vk)
    r = await w.client.post(f"/api/content/{item_id}", json={"action": "publish"}, headers=auth())
    body = await r.json()
    item = body["item"]
    assert r.status == 200 and item["status"] == "published" and [p["platform"] for p in item["published"]] == ["telegram"]
    assert item["publish_errors"] == [{"platform": "vk", "label": "VK", "error": "токен протух: access_token=***"}]
    assert "TOK456" not in json.dumps(body, ensure_ascii=False)
    # то же видно в списке и в карточке
    listed = (await (await w.client.get("/api/content?status=published", headers=auth())).json())["items"][0]
    assert listed["publish_errors"] == item["publish_errors"]
    # «Доотправить»: уходит только туда, куда не ушло
    vk.fail = None
    r = await w.client.post(f"/api/content/{item_id}", json={"action": "publish"}, headers=auth())
    body = await r.json()
    assert [x["platform"] for x in body["results"]] == ["vk"] and (tg.calls, vk.calls) == (1, 2)
    assert body["item"]["publish_errors"] == [] and [p["platform"] for p in body["item"]["published"]] == ["telegram", "vk"]
    # всё вышло — третий раз никуда не уходит
    r = await w.client.post(f"/api/content/{item_id}", json={"action": "publish"}, headers=auth())
    body = await r.json()
    assert body["results"] == [] and "уже опубликован" in body["errors"][0] and (tg.calls, vk.calls) == (1, 2)


async def partly_published(w):
    """Материал вышел в Telegram, а в VK — нет."""
    item_id = await w.factory.save("post", "тема", {"title": "Пост", "body": "Текст поста"})
    tg, vk = Pub("telegram", "Telegram"), Pub("vk", "VK", fail="нет прав на стену")
    use_pubs(w, tg, vk)
    r = await w.client.post(f"/api/content/{item_id}", json={"action": "publish"}, headers=auth())
    assert [e["platform"] for e in (await r.json())["item"]["publish_errors"]] == ["vk"]
    vk.fail = None
    return item_id, tg, vk


async def resend(w, item_id):
    r = await w.client.post(f"/api/content/{item_id}", json={"action": "publish"}, headers=auth())
    assert r.status == 200
    return await r.json()


async def test_resend_keeps_platform_that_is_switched_off(w):
    """VK выключили на время: «Доотправить» туда не шлёт, но и не забывает, что материал туда не ушёл."""
    item_id, tg, vk = await partly_published(w)
    await w.db.set_setting("publish_vk", "0")
    body = await resend(w, item_id)
    assert body["results"] == [] and "выключена" in body["errors"][0] and vk.calls == 1
    assert body["item"]["publish_errors"] == [{"platform": "vk", "label": "VK", "error": "нет прав на стену"}]
    await w.db.set_setting("publish_vk", "1")
    body = await resend(w, item_id)
    assert [x["platform"] for x in body["results"]] == ["vk"] and (tg.calls, vk.calls) == (1, 2)
    assert body["item"]["publish_errors"] == []


async def test_resend_keeps_platform_that_is_disconnected(w):
    item_id, tg, vk = await partly_published(w)
    use_pubs(w, tg)  # VK отключили в /channels
    body = await resend(w, item_id)
    assert body["results"] == [] and "VK" in body["errors"][0] and "не подключена" in body["errors"][0]
    assert [e["platform"] for e in body["item"]["publish_errors"]] == ["vk"]
    use_pubs(w, tg, vk)
    body = await resend(w, item_id)
    assert [x["platform"] for x in body["results"]] == ["vk"] and body["item"]["publish_errors"] == []


async def test_resend_keeps_failed_platforms_when_material_is_not_ready(w, monkeypatch):
    item_id, tg, vk = await partly_published(w)
    monkeypatch.setattr(type(w.factory), "not_ready", lambda self, row: "нет файла картинки")
    body = await resend(w, item_id)
    assert body["results"] == [] and body["errors"] == ["нет файла картинки"] and vk.calls == 1
    assert [e["platform"] for e in body["item"]["publish_errors"]] == ["vk"]


async def test_resend_does_not_post_to_newly_connected_platform(w):
    """«Доотправить» шлёт только туда, куда не ушло, — не на площадку, подключённую позже."""
    item_id, tg, vk = await partly_published(w)
    ok_ru = Pub("ok", "Одноклассники")
    use_pubs(w, tg, ok_ru, vk)
    body = await resend(w, item_id)
    assert [x["platform"] for x in body["results"]] == ["vk"] and (tg.calls, ok_ru.calls, vk.calls) == (1, 0, 2)
    assert [p["platform"] for p in body["item"]["published"]] == ["telegram", "vk"] and body["item"]["publish_errors"] == []


async def test_resend_after_interruption_goes_to_every_platform_left(w):
    """Публикацию оборвали на середине (какие площадки остались — не записано): доотправка идёт на все оставшиеся."""
    from leadhunter.content.store import INTERRUPTED
    item_id = await published_item(w)
    await w.factory.store.update(item_id, publish_errors=[INTERRUPTED])
    tg, vk = Pub("telegram", "Telegram"), Pub("vk", "VK")
    use_pubs(w, tg, vk)
    body = await resend(w, item_id)
    assert [x["platform"] for x in body["results"]] == ["vk"] and (tg.calls, vk.calls) == (0, 1)
    assert body["item"]["publish_errors"] == []


async def test_nothing_published_keeps_draft_and_shows_reasons(w):
    item_id = await w.factory.save("post", "тема", {"title": "Пост", "body": "Текст поста"})
    use_pubs(w, Pub("vk", "VK", fail="нет прав на стену"))
    r = await w.client.post(f"/api/content/{item_id}", json={"action": "publish"}, headers=auth())
    item = (await r.json())["item"]
    assert item["status"] == "draft" and item["published"] == []
    assert item["publish_errors"] == [{"platform": "vk", "label": "VK", "error": "нет прав на стену"}]


def test_cabinet_js_shows_failed_platforms_and_resend_button():
    import os
    from leadhunter.miniapp.server import STATIC
    js = open(os.path.join(STATIC, "app.js"), encoding="utf-8").read()
    assert "publish_errors" in js and "Доотправить" in js and "publishing" in js


async def test_m15_no_ai_hint_and_manual_format(w):
    for kind, topic in (("post", "цены на сайты"), ("magnet", "чек-лист"), ("carousel", "без разделителя")):
        err = await error_of(await w.client.post("/api/content", json={"kind": kind, "topic": topic}, headers=auth()))
        assert "OPENAI_API_KEY" not in err and ".env" not in err and "Подключения" in err and "/setup" in err
    r = await w.client.post("/api/content", json={"kind": "carousel", "topic": "5 ошибок | Нет цены | Нет телефона"}, headers=auth())
    item = (await r.json())["item"]
    assert r.status == 200 and item["kind"] == "carousel" and item["title"] == "5 ошибок" and len(item["images"]) == 3
    slides = json.loads(item["body"])["slides"]
    assert [s["text"] for s in slides] == ["", "Нет цены", "Нет телефона"]


async def test_content_error_text_hides_keys(w, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("GET https://api.example/v1?key=SECRET123&x=1 failed, access_token=TOK456")

    monkeypatch.setattr(w.factory, "create", boom)
    r = await w.client.post("/api/content", json={"kind": "carousel", "topic": "а | б"}, headers=auth())
    err = await error_of(r)
    assert "SECRET123" not in err and "TOK456" not in err and "key=***" in err


# ---------- M16–M17: защита в глубину ----------

async def test_m16_init_data_only_from_header(w):
    init = auth()["X-Init-Data"]
    assert (await w.client.get("/api/me", headers={"X-Init-Data": init})).status == 200
    await error_of(await w.client.get("/api/me", params={"initData": init}), 401)


def test_m16_static_js_does_not_read_init_data_from_address():
    import os
    from leadhunter.miniapp.server import STATIC
    js = open(os.path.join(STATIC, "app.js"), encoding="utf-8").read()
    assert "location.search" not in js and 'get("initData")' not in js


async def test_m16_auth_date_from_future_is_rejected(w):
    now = int(time.time())
    future = sign_init_data({"auth_date": now + 10 * 86400, "user": {"id": OWNER}}, TOKEN)
    assert validate_init_data(future, TOKEN) is None
    assert validate_init_data(sign_init_data({"auth_date": now + 30, "user": {"id": OWNER}}, TOKEN), TOKEN)["id"] == OWNER
    assert validate_init_data(sign_init_data({"auth_date": now, "user": "[1]"}, TOKEN), TOKEN) is None
    await error_of(await w.client.get("/api/me", headers=auth(auth_date=now + 10 * 86400)), 401)
    await error_of(await w.client.get("/api/me", headers={"X-Init-Data": sign_init_data(
        {"auth_date": now, "user": {"id": "777"}}, TOKEN)}), 401)


async def test_m16_security_headers(w):
    slug = await w.magnet()
    for path in ("/app", "/static/app.js", "/static/app.css"):
        h = (await w.client.get(path)).headers
        csp = h["Content-Security-Policy"]
        assert "default-src 'self'" in csp and "script-src 'self' https://telegram.org;" in csp and "object-src 'none'" in csp
        # кабинет открывается во фрейме Telegram Web, поэтому здесь не X-Frame-Options, а список в frame-ancestors
        assert "frame-ancestors 'self' https://*.telegram.org" in csp and "X-Frame-Options" not in h
        assert h["X-Content-Type-Options"] == "nosniff"
    for r in (await w.client.get("/api/me", headers=auth()), await w.client.get("/api/me"), await w.client.get(f"/lp/{slug}"),
              await w.client.post(f"/lp/{slug}", data={"contact": ""}), await w.client.get("/lp/nope"),
              await w.client.get("/api/nope")):
        assert r.headers["X-Frame-Options"] == "DENY", r.url
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"], r.url
        assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Referrer-Policy"] == "no-referrer"
    assert "script-src" not in (await w.client.get(f"/lp/{slug}")).headers["Content-Security-Policy"]  # скрипты на лендинге запрещены


async def test_m17_lead_links_only_http_and_tg(w):
    await w.deps.extra["pipeline"].handle(Lead(source="telegram", external_id="3", contact="@oleg", url="tg://resolve?domain=oleg",
                                               text="Ищу подрядчика: нужен сайт для стоматологии, бюджет 80 000"))
    leads = {l["id"]: l for l in (await (await w.client.get("/api/leads", headers=auth())).json())["leads"]}
    assert leads[1]["url"] == "https://kwork.ru/projects/1" and leads[2]["url"] == "" and leads[3]["url"] == "tg://resolve?domain=oleg"
    assert (await (await w.client.get("/api/leads/2", headers=auth())).json())["lead"]["url"] == ""
    r = await w.client.post("/api/leads/2/status", json={"status": "hot"}, headers=auth())
    assert (await r.json())["lead"]["url"] == ""
    from leadhunter.miniapp.validation import safe_url
    for url in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,<script>", "vbscript:x", "//evil.example", "", None, 5):
        assert safe_url(url) == "", url
    assert safe_url("HTTPS://example.com/a?b=1") == "HTTPS://example.com/a?b=1"
