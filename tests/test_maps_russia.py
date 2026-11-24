"""Карты на всю Россию: справочники не считаются сайтом компании, города и рубрики обходятся по частям."""
import asyncio
import json
import time

import pytest
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer

from leadhunter import niches
from leadhunter.analysis.dossier import competitor_rows, site_from_text, subject
from leadhunter.bot.guard import Guard
from leadhunter.bot.handlers import BotDeps, build_router
from leadhunter.bot.notify import TelegramNotifier
from leadhunter.bot.workspace import Workspace
from leadhunter.cities import RU_CITIES, SITE_RUBRICS, MapsPlan, expand_cities, pair_at
from leadhunter.config import Settings
from leadhunter.db import Database
from leadhunter.hosts import company_site, foreign_host, is_own_site, shared_hosts
from leadhunter.miniapp.auth import sign_init_data
from leadhunter.miniapp.server import MiniAppAPI, build_app
from leadhunter.phrases import PhraseProvider
from leadhunter.pipeline import Pipeline
from leadhunter.scoring import ScoreResult
from leadhunter.sources import yandex_maps_web
from leadhunter.sources.yandex_maps import parse_features
from leadhunter.sources.yandex_maps_web import YandexMapsWebSource, audit_lead
from tests.test_bot_e2e import Env, FakeSession, become_owner

TOKEN = "123456:TEST-TOKEN"
SPRAVKER = "https://saratov.spravker.ru/nogtevye-studii/lav-studio-1.htm"


def org(i, title, urls=None, **kw) -> dict:
    return {"type": "business", "id": str(i), "title": title, "address": f"Саратов, ул. Чапаева, {i}",
            "categories": [{"name": "Ногтевая студия"}], **({"urls": urls} if urls else {}), **kw}


def page(items) -> str:
    return '<script>{"x":1,"items":' + json.dumps(items, ensure_ascii=False) + ',"y":2}</script>'


class FakeFetcher:
    """Вместо карт: на любой запрос отдаёт одну и ту же выдачу и запоминает, что спрашивали."""

    def __init__(self, items, fail_on: int | None = None):
        self.body, self.asked, self.fail_on = page(items), [], fail_on

    async def get(self, url, *, params=None, **kw):
        self.asked.append(params["text"])
        fail = self.fail_on is not None and len(self.asked) == self.fail_on

        class R:
            text = self.body

            def raise_for_status(self):
                if fail:
                    raise RuntimeError("карты не ответили")

        return R()


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "maps.db"))
    await d.connect()
    yield d
    await d.close()


def source(items, cities, queries, *, per_poll=6, db=None, audit=True, audit_per_poll=30, fail_on=None):
    src = YandexMapsWebSource(cities, queries, 3600, None, audit, audit_per_poll, per_poll, db)
    src.fetcher = FakeFetcher(items, fail_on)
    return src


@pytest.fixture
def audited(monkeypatch):
    """Аудит сайтов без сети: у каждого сайта «проблема», список проверенных адресов запоминается."""
    seen = []

    async def fake_audit(site, timeout=15):
        seen.append(site)
        return "без HTTPS"

    monkeypatch.setattr(yandex_maps_web, "audit_site", fake_audit)
    return seen


# --- справочники и соцсети — не сайт компании ---
def test_directory_and_social_links_are_not_own_site():
    assert foreign_host(SPRAVKER) == "spravker.ru" and not is_own_site(SPRAVKER)
    for url in ("https://vk.com/lavstudio", "t.me/lav", "https://2gis.ru/saratov/firm/1", "https://zoon.ru/saratov/x/",
                "https://n12345.yclients.com", "https://taplink.cc/lav", "https://uslugi.yandex.ru/profile/x"):
        assert not is_own_site(url), url
    for url in ("https://lav-studio.ru", "http://shop.example.ru/catalog", "https://www.lilypenguin.ru/",
                "http://ваш-мастеръ.рф/", "https://latka.clients.site/", "https://studio.tilda.ws"):
        assert is_own_site(url) and not foreign_host(url), url
    assert not is_own_site("") and not is_own_site("localhost")


def test_company_site_skips_directory_and_takes_real_site():
    assert company_site(org(1, "Лав", [SPRAVKER])) == ("", "spravker.ru")
    assert company_site(org(1, "Лав", [SPRAVKER, "https://lav-studio.ru/?utm_source=maps"])) == ("https://lav-studio.ru", "")
    assert company_site(org(1, "Лав")) == ("", "")


def test_unknown_directory_is_recognized_by_shared_inner_pages():
    items = [org(1, "Лав", ["https://nails-catalog.example/saratov/lav"]),
             org(2, "Ноготок", ["https://nails-catalog.example/saratov/nogotok"]),
             # сеть филиалов: название одно — это её собственный сайт
             org(3, "Пальчики", ["https://palchiki.ru/saratov/chapaeva"]),
             org(4, "Пальчики", ["https://palchiki.ru/saratov/moskovskaya"]),
             org(5, "Глянец", ["https://glyanec.ru"])]
    shared = shared_hosts(items)
    assert shared == {"nails-catalog.example"}
    assert company_site(items[0], shared) == ("", "nails-catalog.example")
    assert company_site(items[2], shared)[0] == "https://palchiki.ru/saratov/chapaeva"


def test_shared_company_site_with_home_page_link_is_not_a_directory():
    """Было: общий сайт сети с разными вывесками считался справочником — даже у карточки со ссылкой на главную."""
    items = [org(1, "Ресторан Гинза", ["https://ginza.example"]),
             org(2, "Терраса", ["https://ginza.example/spb/terrassa"]),
             org(3, "Мансарда", ["https://ginza.example/spb/mansarda?utm_source=maps"])]
    assert shared_hosts(items) == set()
    assert [company_site(x, shared_hosts(items))[0] for x in items] == [
        "https://ginza.example", "https://ginza.example/spb/terrassa", "https://ginza.example/spb/mansarda"]


def test_competitors_do_not_include_directory_pages():
    """Было: в конкурентах «с сайтом» оказывалась страница saratov.spravker.ru."""
    items = [org(1, "Клиент"), org(2, "Лав", [SPRAVKER]), org(3, "В контакте", ["https://vk.com/nails"]),
             org(4, "Глянец", ["https://glyanec.ru/?yclid=1"], ratingData={"ratingValue": 4.9, "reviewCount": 80})]
    market, rows = competitor_rows(items, org_id="1", domain="")
    assert market == {"total": 3, "with_site": 1}
    assert [r["site"] for r in rows] == ["https://glyanec.ru"]


def test_competitor_count_is_not_cut_by_limit():
    items = [org(i, f"Студия {i}", [f"https://studio{i}.ru"]) for i in range(1, 8)]
    market, rows = competitor_rows(items, org_id="1", domain="", limit=2)
    assert market == {"total": 6, "with_site": 6} and len(rows) == 2


def test_order_link_to_directory_is_not_client_site():
    assert site_from_text(f"Нужен сайт как у {SPRAVKER} или https://lav-studio.ru") == "https://lav-studio.ru"


async def test_old_lead_with_directory_as_site_is_company_without_site(db):
    lead = audit_lead(org(2, "Лав"), SPRAVKER.removesuffix(".htm"), "без HTTPS", "Саратов")
    lead_id = await db.save_lead(lead, 5, ScoreResult(5, [], [], []))
    sub = subject(await db.get_lead(lead_id))
    assert sub["kind"] == "company_nosite" and sub["site"] == ""


async def test_poll_treats_directory_page_as_no_site(audited):
    """Было: страницу справочника бот проверял как сайт компании и присылал «аудит» справочника."""
    items = [org(2, "Лав", [SPRAVKER], phones=[{"number": "+7 900 000-00-00"}]), org(4, "Глянец", ["https://glyanec.ru"]),
             org(5, "Без сайта")]
    leads = await source(items, ["Саратов"], ["ногтевая студия"]).poll()
    by_id = {l.external_id: l for l in leads}
    assert audited == ["https://glyanec.ru"]
    assert by_id["2"].source == "yandex_maps" and "вместо него указана страница на spravker.ru" in by_id["2"].text
    assert by_id["2"].contact == "+7 900 000-00-00" and "site" not in by_id["2"].extra["meta"]
    assert by_id["5"].text.endswith("Сайта в карточке нет.")
    assert by_id["audit:4"].source == "site_audit"


def test_api_source_treats_social_page_as_no_site():
    data = {"features": [
        {"properties": {"CompanyMetaData": {"id": "1", "name": "С сайтом", "url": "https://x.ru"}}},
        {"properties": {"CompanyMetaData": {"id": "2", "name": "Во ВКонтакте", "url": "https://vk.com/nails"}}},
    ]}
    (lead,) = parse_features(data, "ногтевая студия", "Саратов")
    assert lead.external_id == "2" and "страница на vk.com" in lead.text


async def test_site_audit_never_opens_local_address(monkeypatch):
    """Ссылку в карточку вписывает сама компания: адрес из внутренней сети (и переадресация на него) не открывается."""
    from leadhunter import brief
    from leadhunter.audit import audit_site

    asked = []

    async def resolve(host, port):
        asked.append(host)
        return ["127.0.0.1"]

    monkeypatch.setattr(brief, "_resolve", resolve)
    assert await audit_site("http://router.example/admin") == "сайт не открывается (адрес не из интернета)"
    assert asked == ["router.example"]


# --- вся Россия: города и рубрики ---
def test_russia_expands_to_city_list():
    assert len(RU_CITIES) == len(set(RU_CITIES)) >= 90 and RU_CITIES[0] == "Москва" and "Саратов" in RU_CITIES
    assert expand_cities(["Россия"]) == RU_CITIES and expand_cities(["вся РФ"]) == RU_CITIES
    assert expand_cities(["Энгельс", "Саратов", " ", "Саратов"]) == ["Энгельс", "Саратов"]
    mixed = expand_cities(["Маркс", "Россия"])
    assert mixed[0] == "Маркс" and len(mixed) == len(RU_CITIES) + 1


def test_defaults_are_all_russia_and_site_rubrics():
    s = Settings(bot_token="123456:FAKE", _env_file=None)
    assert s.yandex_cities == ["Россия"] and s.yandex_queries == SITE_RUBRICS and s.yandex_per_poll == 6
    assert {"банкетный зал", "стилист", "компьютерный клуб"} <= set(SITE_RUBRICS)  # ниши из кейсов на сайте владельца
    assert niches.NICHES["web"].yandex_queries == SITE_RUBRICS
    with pytest.raises(ValueError, match="от 1 до 30"):
        Settings(bot_token="123456:FAKE", _env_file=None, yandex_per_poll=500)


@pytest.mark.parametrize("cities,queries", [(3, 2), (4, 4), (5, 3), (1, 6), (7, 1)])
def test_rotation_visits_every_pair_once(cities, queries):
    cs, qs = [f"г{i}" for i in range(cities)], [f"р{i}" for i in range(queries)]
    pairs = [pair_at(cs, qs, k) for k in range(cities * queries)]
    assert len(set(pairs)) == cities * queries
    if cities > 1:  # соседние запросы — в разных городах
        assert all(a[0] != b[0] for a, b in zip(pairs, pairs[1:]))


async def test_poll_takes_only_part_and_continues_next_time(db, audited):
    src = source([org(5, "Без сайта")], ["Россия"], ["стилист", "фотостудия", "психолог"], per_poll=4, db=db)
    await src.poll()
    assert src.fetcher.asked == ["стилист Москва", "фотостудия Санкт-Петербург", "психолог Новосибирск", "стилист Екатеринбург"]
    # после перезапуска бот продолжает с того же места, а не с Москвы
    again = source([org(5, "Без сайта")], ["Россия"], ["стилист", "фотостудия", "психолог"], per_poll=4, db=db)
    await again.poll()
    assert again.fetcher.asked[0] == "фотостудия Казань" and len(again.fetcher.asked) == 4
    assert (await db.get_setting("maps_cursor")).startswith("8:")


async def test_small_list_is_polled_whole_every_time(db, audited):
    src = source([org(5, "Без сайта")], ["Тула"], ["стилист", "фотостудия"], per_poll=6, db=db)
    await src.poll()
    await src.poll()
    assert src.fetcher.asked == ["стилист Тула", "фотостудия Тула"] * 2


async def test_failed_poll_does_not_move_on(db, audited):
    src = source([org(5, "Без сайта")], ["Россия"], ["стилист"], per_poll=3, db=db, fail_on=2)
    with pytest.raises(RuntimeError):
        await src.poll()
    assert await db.get_setting("maps_cursor", "") == ""
    src.fetcher = FakeFetcher([org(5, "Без сайта")])
    await src.poll()
    assert src.fetcher.asked[0] == "стилист Москва"


async def test_bot_stopped_during_site_audit_repeats_the_same_requests(db, monkeypatch):
    """Было: место обхода сохранялось до проверки сайтов — остановка бота в этот момент теряла лиды прохода на весь круг."""
    started = asyncio.Event()

    async def slow_audit(site, timeout=15):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(yandex_maps_web, "audit_site", slow_audit)
    src = source([org(4, "Глянец", ["https://glyanec.ru"])], ["Россия"], ["стилист"], per_poll=2, db=db)
    poll = asyncio.create_task(src.poll())
    await started.wait()
    poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll
    assert await db.get_setting("maps_cursor", "") == ""


async def test_new_lists_are_walked_from_their_start(db, audited):
    """Было: после смены рубрик обход продолжался с прежнего места, и начало нового списка откладывалось на весь круг."""
    src = source([org(5, "Без сайта")], ["Россия"], ["стилист", "фотостудия"], per_poll=4, db=db)
    await src.poll()
    assert "пройдено 4 из" in await src.plan.describe()
    await db.set_setting("maps_queries", "юрист, нотариус, психолог")
    assert "пройдено 0 из" in await src.plan.describe()
    src.fetcher = FakeFetcher([org(5, "Без сайта")])
    await src.poll()
    assert src.fetcher.asked[0] == "юрист Москва"
    # те же списки, записанные по-другому, — обход не сбрасывается
    await db.set_setting("maps_queries", "юрист;нотариус ,  психолог")
    assert "пройдено 4 из" in await src.plan.describe()


async def test_cabinet_lists_override_env_and_empty_returns_to_env(db):
    plan = MapsPlan(["Россия"], ["стилист"], 6, db)
    await db.set_setting("maps_cities", "Саратов, Энгельс")
    await db.set_setting("maps_queries", "юрист; нотариус")
    assert await plan.lists() == (["Саратов", "Энгельс"], ["юрист", "нотариус"])
    assert "Все 4 запросов — за один проход" in await plan.describe()
    await db.set_setting("maps_cities", "")
    assert (await plan.lists())[0] == RU_CITIES
    assert f"пройдено 0 из {len(RU_CITIES) * 2}" in await plan.describe()


async def test_audit_limit_is_shared_between_requests(audited):
    items = [org(i, f"Студия {i}", [f"https://studio{i}.ru"]) for i in range(1, 6)]
    src = source(items, ["Тула", "Калуга"], ["стилист"], per_poll=2, audit_per_poll=4)
    leads = await src.poll()
    assert sorted(l.extra["meta"]["city"] for l in leads) == ["Калуга", "Калуга", "Тула", "Тула"]


async def test_niche_rubrics_reach_maps(db):
    plan = MapsPlan(["Тула"], ["кофейня"], 6, db)
    await niches.apply(db, PhraseProvider(db, ["нужен сайт"]), niches.NICHES["web"])
    assert (await plan.lists())[1] == SITE_RUBRICS


# --- бот и кабинет ---
@pytest.fixture
async def env(db):
    session = FakeSession()
    bot = Bot("1:TEST", session=session)
    notifier = TelegramNotifier(bot, db, [], hot_score=8)
    guard = Guard(bot, db, [])
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), Workspace(bot, db), None, "профиль", 0, 24)
    dp = Dispatcher()
    dp.include_router(build_router(deps))
    e = Env(bot, dp, session, db, notifier, Pipeline(db, notifier, default_threshold=5))
    e.guard, e.deps = guard, deps
    guard.new_claim_code()
    yield e
    await bot.session.close()


async def test_bot_maps_command(env):
    await become_owner(env)
    assert "не запущен" in (await env.cmd("/maps"))[0]
    env.deps.sources.append(source([], ["Россия"], ["стилист", "психолог"], db=env.db))
    text = (await env.cmd("/maps"))[0]
    assert "Москва" in text and f"и ещё {len(RU_CITIES) - 8}" in text and "стилист, психолог" in text
    assert "Саратов, Энгельс" in (await env.cmd("/maps города Саратов,Энгельс"))[0]
    assert await env.db.get_setting("maps_cities") == "Саратов, Энгельс"
    assert "юрист" in (await env.cmd("/maps рубрики юрист"))[0]
    assert "стилист, психолог" in (await env.cmd("/maps рубрики сброс"))[0]
    assert "Не понял" in (await env.cmd("/maps Саратов"))[0] and await env.db.get_setting("maps_cities") == "Саратов, Энгельс"


async def test_cabinet_maps_settings(db):
    guard = Guard(None, db, [777])
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), Workspace(None, db), None, "профиль")
    head = {"X-Init-Data": sign_init_data({"auth_date": int(time.time()), "query_id": "q",
                                           "user": {"id": 777, "first_name": "Стас"}}, TOKEN)}
    async with TestClient(TestServer(build_app(MiniAppAPI(db, TOKEN, guard, deps, [])))) as c:
        assert (await (await c.get("/api/settings", headers=head)).json())["maps"] is None
        deps.sources.append(source([], ["Россия"], ["стилист"], db=db))
        got = (await (await c.get("/api/settings", headers=head)).json())["maps"]
        assert got["cities"] == "Россия" and got["queries"] == "стилист" and "Городов" in got["status"]
        r = await c.post("/api/settings", headers=head, json={"maps_cities": "Саратов;  Энгельс", "maps_queries": "юрист"})
        assert (await r.json())["maps"] == {"cities": "Саратов, Энгельс", "queries": "юрист",
                                            "status": "Городов: 2, рубрик: 1. Все 2 запросов — за один проход."}
        assert (await c.post("/api/settings", headers=head, json={"maps_cities": 5})).status == 400
        r = await c.post("/api/settings", headers=head, json={"maps_cities": ""})
        assert (await r.json())["maps"]["cities"] == "Россия"
