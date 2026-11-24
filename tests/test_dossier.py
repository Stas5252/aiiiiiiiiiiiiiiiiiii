"""Досье на лид: разбор сайта, место в поиске, конкуренты, вывод ИИ; бот, кабинет и автозапуск."""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer

from leadhunter import brief
from leadhunter.analysis import fetch as fetch_mod
from leadhunter.analysis.dossier import (AUTO_PER_HOUR, SYSTEM, DossierError, DossierService, competitor_rows, has_subject,
                                         site_from_text, subject)
from leadhunter.analysis.fetch import Page
from leadhunter.analysis.render import LIMIT, render
from leadhunter.analysis.search import position
from leadhunter.analysis.site import analyze_site, detect_cms, domain_of, footer_year, parse_page, robots_allows
from leadhunter.bot.callbacks import build_callbacks
from leadhunter.bot.dossier import auto_dossier, build_dossier_router
from leadhunter.bot.guard import Guard
from leadhunter.bot.handlers import BotDeps, build_router
from leadhunter.bot.notify import TelegramNotifier, new_lead_keyboard, taken_keyboard
from leadhunter.bot.workspace import Workspace
from leadhunter.db import Database
from leadhunter.http import CaptchaError
from leadhunter.miniapp.auth import sign_init_data
from leadhunter.miniapp.server import MiniAppAPI, build_app
from leadhunter.models import Lead
from leadhunter.phrases import PhraseProvider
from leadhunter.pipeline import Enriched, Pipeline
from leadhunter.scoring import ScoreResult
from leadhunter.sources.yandex_maps_web import audit_lead, business_lead, parse_yandex_items
from tests.test_bot_e2e import OTHER, OWNER, Env, FakeSession, become_owner

FIXTURE = (Path(__file__).parent / "fixtures" / "yandex_maps_search.html").read_text()
NOW = datetime(2026, 11, 24)

GOOD = """<!doctype html><html><head><title>Ремонт обуви в Туле — мастерская «Каблук»</title>
<meta name="viewport" content="width=device-width"><meta name="description" content="Чиним обувь за день. Гарантия.">
<script type="application/ld+json">{"@type":"LocalBusiness"}</script><link href="/wp-content/themes/x/style.css">
</head><body><h1>Ремонт обуви за один день</h1><form action="/order"><input name="phone"></form>
<a href="https://wa.me/79001234567">WhatsApp</a><p>+7 900 123-45-67</p>""" + "<p>текст</p>" * 300 + """
<footer>© 2019–2026 Каблук</footer></body></html>"""
BAD = "<html><head></head><body>" + "<p>Добро пожаловать на наш сайт</p>" * 80 + "<div>&copy; 2017 ООО Ромашка</div></body></html>"
ROBOTS_OK = "User-agent: *\nDisallow: /admin/\nSitemap: https://kabluk.ru/sitemap.xml\n"


class FakeWeb:
    """Подменяет загрузку страниц: адрес → Page. Неизвестный адрес — 404. Запоминает, что запрашивали."""

    def __init__(self, pages: dict[str, Page | str]):
        self.pages, self.asked = pages, []

    async def __call__(self, url, **kw):
        self.asked.append(url)
        page = self.pages.get(url)
        if page is None:
            return Page(url, 404, "not found")
        return page if isinstance(page, Page) else Page(url, 200, page, 0.4)


class FakeLLM:
    model = "fake"

    def __init__(self, reply=None, fail=False):
        self.reply = reply or {"pain": "Сайт без HTTPS и не открывается с телефона.", "offer": "Новый сайт под ключ",
                               "price": "от 25 000 ₽", "questions": ["Сколько заявок даёт сайт сейчас?"],
                               "message": "Здравствуйте! Посмотрел ваш сайт <b>: он без HTTPS. Обсудим?"}
        self.fail, self.calls = fail, []

    async def complete_json(self, system, user, **kw):
        self.calls.append((system, user))
        if self.fail:
            raise RuntimeError("503")
        return self.reply


class FakeMaps:
    def __init__(self, body=FIXTURE, error=None):
        self.body, self.error, self.asked = body, error, []

    async def get(self, url, *, params=None, **kw):
        self.asked.append(params["text"])
        if self.error:
            raise self.error

        class R:
            text = self.body

            def raise_for_status(self):
                pass

        return R()


async def fake_search(query, key):
    return "бесплатный поиск", ["https://2gis.ru/tula", "https://zoon.ru/tula", "https://www.kabluk.ru/price"]


def company_row_lead(site="kabluk.ru") -> Lead:
    item = {"id": "999", "title": "Каблук", "address": "Тула, ул. Ленина, 1", "categories": [{"name": "Ремонт обуви"}],
            "ratingData": {"ratingValue": 4.6, "reviewCount": 37}, "phones": [{"number": "+7 900 000-00-00"}]}
    return audit_lead(item, site, "без HTTPS", "Тула") if site else business_lead(item, "ремонт обуви", "Тула")


async def save(db, lead: Lead, score=6) -> int:
    return await db.save_lead(lead, score, ScoreResult(score, [], [], []))


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "d.db"))
    await d.connect()
    yield d
    await d.close()


def service(db, *, web=None, llm=None, maps=None, search=fake_search) -> DossierService:
    web = web or FakeWeb({"https://kabluk.ru/robots.txt": ROBOTS_OK, "https://kabluk.ru": GOOD})
    svc = DossierService(db, lambda: llm, get=web, search=search, maps=maps or FakeMaps(), default_profile="Делаю сайты")
    svc.web = web
    return svc


# --- разбор страницы ---
def test_good_page_has_facts_and_no_problems():
    r = parse_page(Page("https://kabluk.ru/", 200, GOOD, 0.4), now=NOW)
    assert r["problems"] == []
    assert r["title"] == "Ремонт обуви в Туле — мастерская «Каблук»" and r["h1"] == "Ремонт обуви за один день"
    assert r["https"] and r["mobile"] and r["schema"] and r["forms"] == 1
    assert r["cms"] == "WordPress" and r["year"] == 2026 and r["messengers"] == ["WhatsApp"]
    assert r["phones"] == ["+7 900 123-45-67"]


def test_bad_page_lists_problems_in_plain_words():
    r = parse_page(Page("http://romashka.ru/", 200, BAD, 3.4), now=NOW)
    assert r["problems"] == [
        "без HTTPS", "не адаптирован под телефон", "отвечает медленно (3.4 с)", "нет заголовка страницы (title)",
        "нет описания для поиска (description)", "нет главного заголовка (h1)",
        "на главной нет ни формы заявки, ни телефона, ни мессенджера", "в подвале 2017 год — сайт давно не обновляли"]


@pytest.mark.parametrize("body", [
    "<meta " * 100_000, "<title>" * 85_000, "<h1>" * 150_000, "<" * 600_000, "a" * 600_000 + "@x.ru", "8 " * 300_000,
    "<meta name=" * 50_000, '<meta content="' * 40_000, "<meta content='" * 40_000, '<meta "' + "a" * 590_000, "© " * 300_000, "<form" * 120_000, " \n" * 300_000,
])
def test_hostile_page_is_parsed_in_linear_time(body):
    """Сайт по ссылке из заказа пишет посторонний: мусорная страница не должна подвешивать бота."""
    started = time.monotonic()
    parse_page(Page("https://x.ru/", 200, body, 0.1), now=NOW)
    assert time.monotonic() - started < 2


async def test_hostile_robots_is_read_in_linear_time():
    web = FakeWeb({"https://x.ru/robots.txt": " \n" * 300_000, "https://x.ru": GOOD})
    started = time.monotonic()
    assert (await analyze_site("https://x.ru", get=web, now=NOW))["ok"]
    assert time.monotonic() - started < 2


def test_meta_tags_are_read_in_any_attribute_order_and_quotes():
    body = """<meta content='Чиним обувь' name="Description"><meta name=viewport content="width=device-width">
    <META NAME="generator" CONTENT="Drupal 10"><h1 class="x"><span>Ремонт</span> обуви</h1><h1>Второй</h1><title>Каблук</title>"""
    r = parse_page(Page("https://x.ru/", 200, body, 0.1), now=NOW)
    assert (r["description"], r["mobile"], r["cms"], r["h1"], r["h1_count"], r["title"]) == (
        "Чиним обувь", True, "Drupal 10", "Ремонт обуви", 2, "Каблук")
    body = '<meta name="description" content="Главная > Ремонт обуви"><meta name=viewport content=width=device-width>'
    r = parse_page(Page("https://x.ru/", 200, body, 0.1), now=NOW)
    assert r["description"] == "Главная > Ремонт обуви" and r["mobile"]
    assert parse_page(Page("https://x.ru/", 200, "<p>Пишите: anna@firma.ru, тел. 8 (900) 123-45-67</p>", 0.1))["emails"] == ["anna@firma.ru"]
    page = Page("https://x.ru/", 200, "<p>Почта: anna@firma.ru,boss@firma.ru;sales@firma.ru</p>", 0.1)
    assert parse_page(page)["emails"] == ["anna@firma.ru", "boss@firma.ru"]  # храним первые две


@pytest.mark.parametrize("body, year", [("© 2015–2019 Фирма", 2019), ("Copyright 2024", 2024), ("&copy; ООО, 2021 г.", 2021),
                                        ("основана в 1998 году", None), ("<p>© Фирма</p><p>2010</p>", None)])
def test_footer_year_is_the_latest_one_next_to_copyright(body, year):
    assert footer_year(body) == year


def test_last_year_in_footer_is_not_a_problem():
    page = Page("https://x.ru/", 200, GOOD.replace("2019–2026", "2025"), 0.2)
    assert not any("подвале" in p for p in parse_page(page, now=NOW)["problems"])


@pytest.mark.parametrize("body, cms", [('<script src="https://static.tildacdn.com/js/t.js">', "Tilda"),
                                       ('<link href="/bitrix/templates/main/s.css">', "1С-Битрикс"),
                                       ('<meta name="generator" content="Drupal 10">', "Drupal 10"), ("<p>просто текст</p>", "")])
def test_cms_is_detected(body, cms):
    assert detect_cms(body) == cms


def test_domain_of_strips_www_and_path():
    assert domain_of("https://www.Kabluk.ru/price?x=1") == "kabluk.ru" and domain_of("kabluk.ru") == "kabluk.ru"


# --- разбор сайта: robots.txt, карта сайта, ошибки ---
async def test_site_closed_in_robots_is_not_read_and_reported():
    web = FakeWeb({"https://secret.ru/robots.txt": "User-agent: *\nDisallow: /\n", "https://secret.ru": GOOD})
    r = await analyze_site("secret.ru", get=web)
    assert web.asked == ["https://secret.ru/robots.txt"]  # главную не запрашивали
    assert not r["ok"] and r["problems"] == ["закрыт от поисковиков в robots.txt"]


@pytest.mark.parametrize("robots, url, allowed", [
    ("User-agent: *\nDisallow: /\nAllow: /$\n", "https://x.ru", True),  # открыта только главная
    ("User-agent: *\nDisallow: /\nAllow: /$\n", "https://x.ru/price", False),
    ("User-agent: *\nDisallow: /*\n", "https://x.ru/", False),  # «/*» — то же, что «/»
    ("User-agent: *\nDisallow: /\nAllow: /tula/\n", "https://x.ru/tula/shop", True),  # точнее — значит главнее
    ("User-agent: *\nAllow: /\nDisallow: /\n", "https://x.ru/", True),  # поровну — побеждает разрешение
    ("User-agent: *\nDisallow: /*?\nDisallow: /*.pdf$\n", "https://x.ru/", True),
    ("User-agent: *\nDisallow: /*?\n", "https://x.ru/catalog?page=2", False),
    ("User-agent: *\nDisallow:\n", "https://x.ru/", True),  # пустой запрет ничего не запрещает
    ("User-agent: Yandex\nDisallow: /\n\nUser-agent: *\nDisallow: /admin # служебное\n", "https://x.ru/", True),
    ("User-agent: Googlebot\nUser-agent: *\nDisallow: /\n", "https://x.ru/", False),  # одна группа на двух роботов
    ("User-agent: *\nDisallow: /\n\nUser-agent: LeadHunterBot\nAllow: /\n", "https://x.ru/", True),
    ("", "https://x.ru/", True),
    ("User-agent: *\nDisallow: /каталог/\n", "https://x.ru/%D0%BA%D0%B0%D1%82%D0%B0%D0%BB%D0%BE%D0%B3/1", False),
    ("User-agent: *\nDisallow: /%D0%BA%D0%B0%D1%82%D0%B0%D0%BB%D0%BE%D0%B3/\n", "https://x.ru/каталог/1", False),
    ("User-agent: *\nDisallow: /каталог/\n", "https://x.ru/%D1%86%D0%B5%D0%BD%D1%8B", True),
    ("User-agent: *\nDisallow: /a/b\n", "https://x.ru/a%2Fb", True),  # «%2F» — не «/»: это другой адрес
    ("User-agent: *\nDisallow: /a%2fb\n", "https://x.ru/a%2Fb", False),  # регистр %-кода не важен
    ("User-agent: *\nDisallow: /%41dmin\n", "https://x.ru/Admin", False),
    ("User-agent: *\nDisallow: /search?q\n", "https://x.ru/search%3Fq", True),
    # исключение длиннее запрета, как бы они ни были записаны
    ("User-agent: *\nDisallow: /%D0%BA%D0%B0%D1%82%D0%B0%D0%BB%D0%BE%D0%B3/\nAllow: /каталог/обувь\n", "https://x.ru/каталог/обувь", True),
    ("User-agent: *\nDisallow: /каталог/\nAllow: /%D0%BA%D0%B0%D1%82%D0%B0%D0%BB%D0%BE%D0%B3/a\n", "https://x.ru/каталог/a", True),
])
def test_robots_rules_are_read_like_search_engines_do(robots, url, allowed):
    assert robots_allows(robots, url) is allowed


@pytest.mark.parametrize("pattern, path, matches", [
    ("/a*b*c", "/a1b2c3", True), ("/a*b*c$", "/a1b2c3", False), ("/a*b*c$", "/a1b2c", True), ("/*.pdf$", "/x/y.pdf", True),
    ("/*.pdf$", "/x/y.pdf?1", False), ("/a*a$", "/a", False), ("/a*a$", "/aa", True), ("/a$", "/a", True), ("/a$", "/ab", False),
    ("/a**b", "/ab", True), ("*", "/anything", True), ("/x*", "/y", False),
])
def test_robots_wildcards(pattern, path, matches):
    from leadhunter.analysis.site import _rule_matches

    assert _rule_matches(pattern, path) is matches


def test_hostile_robots_pattern_does_not_hang_the_bot():
    """Ссылку и robots.txt задаёт автор заказа: шаблон из сотни «*a» на длинном адресе раньше считался бы часами."""
    robots = "User-agent: *\nDisallow: /" + "*a" * 100 + "$\n"
    started = time.monotonic()
    assert robots_allows(robots, "https://x.ru/" + "a" * 5000 + "b") is True
    assert time.monotonic() - started < 1


async def test_sitemap_from_robots_saves_a_request():
    web = FakeWeb({"https://kabluk.ru/robots.txt": ROBOTS_OK, "https://kabluk.ru": GOOD})
    r = await analyze_site("https://kabluk.ru", get=web, now=NOW)
    assert r["ok"] and r["robots"] and r["sitemap"] and r["problems"] == [] and len(web.asked) == 2


@pytest.mark.parametrize("line", ["Sitemap", "Sitemap:", "# Sitemap: https://kabluk.ru/sitemap.xml"])
async def test_sitemap_line_without_address_is_not_a_sitemap(line):
    web = FakeWeb({"https://kabluk.ru/robots.txt": f"User-agent: *\nDisallow: /admin/\n{line}\n", "https://kabluk.ru": GOOD})
    r = await analyze_site("https://kabluk.ru", get=web, now=NOW)
    assert not r["sitemap"] and r["problems"] == ["нет карты сайта (sitemap.xml)"] and "https://kabluk.ru/sitemap.xml" in web.asked


async def test_missing_robots_and_sitemap_are_problems():
    r = await analyze_site("https://kabluk.ru", get=FakeWeb({"https://kabluk.ru": GOOD}), now=NOW)
    assert r["problems"] == ["нет robots.txt", "нет карты сайта (sitemap.xml)"]


async def test_html_page_instead_of_robots_is_not_robots():
    web = FakeWeb({"https://kabluk.ru/robots.txt": "<html><body>404</body></html>", "https://kabluk.ru": GOOD,
                   "https://kabluk.ru/sitemap.xml": "<?xml version='1.0'?><urlset></urlset>"})
    r = await analyze_site("https://kabluk.ru", get=web, now=NOW)
    assert not r["robots"] and r["sitemap"] and r["problems"] == ["нет robots.txt"]


async def test_dead_site_is_a_fact_not_an_exception():
    web = FakeWeb({"https://dead.ru": Page("https://dead.ru", error="сайт не открывается (ConnectionError)")})
    r = await analyze_site("dead.ru", get=web)
    assert not r["ok"] and r["problems"] == ["сайт не открывается (ConnectionError)"]
    r = await analyze_site("https://err.ru", get=FakeWeb({"https://err.ru": Page("https://err.ru", 503, "x")}))
    assert r["error"] == "отдаёт ошибку 503" and r["problems"] == ["сайт отдаёт ошибку 503"]


async def test_local_address_from_lead_text_is_never_requested(monkeypatch):
    async def resolve(host, port):
        return ["127.0.0.1"]

    monkeypatch.setattr(brief, "_resolve", resolve)
    page = await fetch_mod.fetch("http://internal.example/admin")
    assert page.error == "адрес не из интернета" and not page.ok


# --- о ком досье ---
@pytest.mark.parametrize("text, site", [
    ("Нужно доработать сайт https://kabluk.ru/price, добавить форму.", "https://kabluk.ru/price"),
    ("ТЗ тут https://docs.google.com/document/d/1 а сайт вот: romashka.ru.", "romashka.ru"),
    ("Макет https://www.figma.com/file/x, пишите в https://t.me/anna", ""),
    ("Нужен лендинг под ключ, бюджет 30 000 руб", ""),
    ("Писать на почту anna@firma.ru", ""),
    ("Перенести www.старый-сайт.рф на Tilda", "www.старый-сайт.рф"),
    ("Нужен парсер на Node.js и Vue.js, опыт с ASP.NET", ""),
    ("Доработать shop.example.ru", "shop.example.ru"),
    ("Магазин на my.shop.example.ru, CMS своя", "my.shop.example.ru"),
    ("Почта anna.petrova@mail.firma.ru", ""),
    ("Сайт сейчас на shop.brand.ru.com, нужен переезд", ""),  # зона не из списка — лучше не найти, чем обрезать адрес
    ("Сайт romashka.ru.", "romashka.ru"),
])
def test_client_site_is_taken_from_order_text(text, site):
    assert site_from_text(text) == site


async def test_subject_of_company_comes_from_source_meta(db):
    row = await db.get_lead(await save(db, company_row_lead()))
    s = subject(row)
    assert (s["kind"], s["site"], s["category"], s["city"], s["org_id"]) == ("company_site", "kabluk.ru", "Ремонт обуви", "Тула", "999")
    assert s["rating"] == 4.6 and s["reviews"] == 37 and not s["site_from_text"]
    row = await db.get_lead(await save(db, company_row_lead(site="")))
    assert subject(row)["kind"] == "company_nosite" and subject(row)["site"] == ""


async def test_subject_of_old_company_lead_without_meta_is_read_from_text(db):
    lead = Lead(source="site_audit", external_id="audit:5", title="Каблук",
                text="Ремонт обуви, изготовление ключей, Тула. Сайт kabluk.ru: без HTTPS. Повод предложить новый сайт.")
    s = subject(await db.get_lead(await save(db, lead)))
    assert (s["kind"], s["site"], s["category"], s["city"], s["org_id"]) == ("company_site", "kabluk.ru", "Ремонт обуви", "Тула", "5")


async def test_old_company_lead_with_scheme_in_site_keeps_whole_address(db):
    lead = Lead(source="site_audit", external_id="audit:6", title="Топ-Мастер",
                text="Ремонт обуви, Тула. Сайт http://obuvs.ru: без HTTPS. Повод предложить новый сайт.")
    assert subject(await db.get_lead(await save(db, lead)))["site"] == "http://obuvs.ru"


async def test_subject_of_order(db):
    row = await db.get_lead(await save(db, Lead(source="kwork", external_id="1", title="Доработать сайт", text="Сайт: kabluk.ru")))
    assert subject(row)["kind"] == "order" and subject(row)["site_from_text"] and has_subject(row)
    row = await db.get_lead(await save(db, Lead(source="kwork", external_id="2", title="Нужен бот", text="Без ссылок")))
    assert not has_subject(row)


def test_competitors_are_other_companies_with_sites():
    items = parse_yandex_items(FIXTURE)
    market, rows = competitor_rows(items, org_id="1252970969", domain="obuvs.ru")
    assert market == {"total": 23, "with_site": 11}
    assert len(rows) == 4 and all(r["site"] and "obuvs.ru" not in r["site"] for r in rows)
    assert rows[0]["title"] == "SneakNFresh" and rows[0]["rating"] == 5 and rows[0]["reviews"] == 291
    assert "?" not in rows[0]["site"]  # метки рекламы из адреса убраны


def test_search_position():
    urls = ["https://2gis.ru/x", "https://shop.kabluk.ru/", ""]
    assert position("kabluk.ru", urls) == 2 and position("nokabluk.ru", urls) is None
    assert position("ваш-мастер.рф", ["https://" + "ваш-мастер.рф".encode("idna").decode() + "/price"]) == 1


# --- сборка досье ---
async def test_company_with_site_gets_full_dossier(db):
    lead_id = await save(db, company_row_lead())
    llm = FakeLLM()
    svc = service(db, llm=llm)
    d = await svc.build(lead_id)
    assert d["kind"] == "company_site" and d["site"]["ok"] and d["site"]["domain"] == "kabluk.ru"
    assert d["seo"] == {"query": "Ремонт обуви Тула", "engine": "бесплатный поиск", "position": 3, "depth": 3}
    assert d["market"] == {"query": "Ремонт обуви Тула", "total": 24, "with_site": 12} and len(d["competitors"]) == 4
    assert all("report" in c for c in d["competitors"])
    assert d["geo"] == {"city": "Тула", "address": "Тула, ул. Ленина, 1", "category": "Ремонт обуви", "rating": 4.6, "reviews": 37}
    assert d["ai"]["offer"] == "Новый сайт под ключ" and d["notes"] == []
    assert svc.maps.asked == ["Ремонт обуви Тула"]
    assert await db.get_dossier(lead_id) == d


async def test_ai_gets_brief_and_facts_but_not_min_order(db):
    await brief.save(db, {"services": "Сайты под ключ", "prices": "Сайт от 25 000 ₽", "min_budget": "10000", "contact": "@stas"})
    lead_id = await save(db, company_row_lead())
    llm = FakeLLM()
    await service(db, llm=llm).build(lead_id)
    system, user = llm.calls[0]
    assert "Сайт от 25 000 ₽" in system and "@stas" in system and "Минимальный заказ" not in system
    facts = json.loads(user.split("Досье:\n", 1)[1])
    assert facts["сайт"]["domain"] == "kabluk.ru" and facts["сайт"]["откуда"] == "карточка компании на картах"
    assert facts["место в поиске"]["position"] == 3 and len(facts["конкуренты"]) == 4
    assert "не выдумывай" in SYSTEM and "сумм не называй" in SYSTEM


async def test_company_without_site_gets_competitors_and_no_seo(db):
    lead_id = await save(db, company_row_lead(site=""))
    svc = service(db, llm=FakeLLM())
    d = await svc.build(lead_id)
    assert d["kind"] == "company_nosite" and d["site"] is None and d["seo"] is None
    assert d["market"]["with_site"] == 12 and len(d["competitors"]) == 4 and d["ai"]


async def test_order_with_link_is_marked_as_possibly_not_clients_site(db):
    lead_id = await save(db, Lead(source="kwork", external_id="1", title="Доработать сайт", text="Сайт https://kabluk.ru"))
    llm = FakeLLM()
    svc = service(db, llm=llm)
    d = await svc.build(lead_id)
    assert d["kind"] == "order" and d["site"]["ok"] and d["site_from_text"] and d["market"] is None and d["geo"] is None
    assert svc.maps.asked == [] and any("может быть пример" in n for n in d["notes"])
    assert json.loads(llm.calls[0][1].split("Досье:\n", 1)[1])["сайт"]["откуда"] == "ссылка из текста заказа"


async def test_order_without_link_gets_ai_only_and_needs_ai(db):
    lead_id = await save(db, Lead(source="kwork", external_id="1", title="Нужен бот", text="Бот для записи клиентов"))
    d = await service(db, llm=FakeLLM()).build(lead_id)
    assert d["site"] is None and d["ai"]["message"] and any("нет ссылки на сайт" in n for n in d["notes"])
    await db.conn.execute("DELETE FROM dossiers")
    with pytest.raises(DossierError, match="ИИ не подключён"):
        await service(db).build(lead_id)
    assert await db.get_dossier(lead_id) is None


async def test_without_ai_dossier_is_technical_and_says_so(db):
    lead_id = await save(db, company_row_lead())
    d = await service(db).build(lead_id)
    assert d["site"]["ok"] and d["ai"] is None and any("ИИ не подключён" in n for n in d["notes"])


async def test_ai_failure_keeps_technical_part(db):
    lead_id = await save(db, company_row_lead())
    d = await service(db, llm=FakeLLM(fail=True)).build(lead_id)
    assert d["site"]["ok"] and d["competitors"] and d["ai"] is None and any("ИИ не ответил" in n for n in d["notes"])


async def test_slow_ai_does_not_hold_the_dossier(db, monkeypatch):
    from leadhunter.analysis import dossier

    class SlowLLM(FakeLLM):
        async def complete_json(self, system, user, **kw):
            await asyncio.sleep(30)

    monkeypatch.setattr(dossier, "AI_TIMEOUT", 0.05)
    d = await asyncio.wait_for(service(db, llm=SlowLLM()).build(await save(db, company_row_lead())), 5)
    assert d["site"]["ok"] and d["ai"] is None and any("ИИ не ответил" in n for n in d["notes"])


async def test_maps_captcha_and_dead_search_are_notes_not_failures(db):
    async def dead_search(query, key):
        raise RuntimeError("timeout")

    lead_id = await save(db, company_row_lead())
    d = await service(db, llm=FakeLLM(), maps=FakeMaps(error=CaptchaError("капча")), search=dead_search).build(lead_id)
    assert d["site"]["ok"] and d["market"] is None and d["competitors"] == [] and d["seo"] is None
    assert any("капчу" in n for n in d["notes"]) and any("Поисковик не ответил" in n for n in d["notes"])


async def test_ready_dossier_and_site_report_are_not_rebuilt(db):
    first, second = await save(db, company_row_lead()), await save(db, Lead(source="kwork", external_id="7", text="сайт kabluk.ru"))
    svc = service(db, llm=FakeLLM())
    d = await svc.build(first)
    asked, maps_asked = len(svc.web.asked), len(svc.maps.asked)
    assert await svc.build(first) == d and len(svc.web.asked) == asked and len(svc.maps.asked) == maps_asked
    await svc.build(second)  # тот же сайт у другого лида — берётся готовый разбор
    assert len(svc.web.asked) == asked
    await svc.build(first, force=True)  # «Обновить» — сайт клиента перечитывается
    assert len(svc.web.asked) == asked + 2 and len(svc.maps.asked) == maps_asked + 1


async def test_old_site_report_is_rechecked(db):
    await db.save_site_report("kabluk.ru", {"domain": "kabluk.ru", "ok": True, "problems": ["старый разбор"]})
    await db.conn.execute("UPDATE site_reports SET created_at = '2020-01-01T00:00:00+00:00'")
    assert await db.get_site_report("kabluk.ru", 7) is None
    svc = service(db)
    assert (await svc.site_report("kabluk.ru"))["problems"] == [] and svc.web.asked


async def test_auto_dossier_is_limited_per_hour(db):
    svc = service(db)
    assert [svc.auto_allowed() for _ in range(AUTO_PER_HOUR + 2)] == [True] * AUTO_PER_HOUR + [False, False]


# --- текст досье для Telegram ---
async def test_render_has_all_sections_and_escapes_html(db):
    lead_id = await save(db, company_row_lead())
    text = render(lead_id, await service(db, llm=FakeLLM()).build(lead_id))
    for part in ("🔎 <b>Досье на лид #1</b> · Каблук", "🌐 <b>Сайт</b> kabluk.ru", "на 3 месте (бесплатный поиск)", "Есть: HTTPS, мобильная версия, robots.txt, карта сайта, микроразметка, форма заявки",
                 "из 24 компаний по запросу «Ремонт обуви Тула» сайт есть у 12", "📍 <b>Гео</b>: Тула, ул. Ленина, 1 · ★4.6 (37)",
                 "Что предложить: Новый сайт под ключ", "Цена: от 25 000 ₽",
                 "<code>Здравствуйте! Посмотрел ваш сайт &lt;b&gt;: он без HTTPS. Обсудим?</code>"):
        assert part in text, part
    assert len(text) <= LIMIT


def test_render_fits_telegram_limit_and_keeps_message():
    long = "очень длинный текст " * 60
    d = {"title": "Фирма", "site": {"domain": "x.ru", "ok": True, "problems": [long] * 5, "seconds": 1.0, "kb": 10, "title": long},
         "seo": {"query": "q", "engine": "Google", "position": None, "depth": 20}, "market": {"query": "q", "total": 10, "with_site": 5},
         "competitors": [{"title": long, "site": "c.ru", "report": {"ok": True, "problems": [long] * 3}}] * 4,
         "geo": {"city": "Тула"}, "ai": {"pain": long[:600], "offer": "x", "price": "", "questions": [], "message": "Привет! " * 150},
         "notes": [long]}
    text = render(1, d)
    assert len(text) <= LIMIT and "✍️ <b>Первое сообщение</b>" in text and "Привет! Привет!" in text
    assert "🏁" not in text  # при нехватке места первыми уходят конкуренты и гео, сообщение остаётся


def test_render_of_unreachable_site():
    text = render(3, {"site": {"domain": "dead.ru", "ok": False, "error": "сайт не открывается (Timeout)"}, "notes": ["ИИ не подключён"]})
    assert "⚠️ сайт не открывается (Timeout)" in text and "ℹ️ ИИ не подключён" in text and "Нет:" not in text


def test_render_names_what_site_lacks_and_how_deep_search_looked():
    d = {"site": {"domain": "x.ru", "ok": True, "problems": [], "seconds": 1.0, "kb": 10, "https": True, "mobile": True},
         "seo": {"query": "ремонт обуви Тула", "engine": "бесплатный поиск", "position": None, "depth": 7},
         "geo": {"city": "Тула", "address": "ул. Ленина, 1"}}
    text = render(1, d)
    assert "Есть: HTTPS, мобильная версия\nНет: robots.txt, карта сайта, микроразметка, форма заявки" in text
    assert "сайт в первых 7 результатах не найден (бесплатный поиск)" in text and "📍 <b>Гео</b>: Тула, ул. Ленина, 1" in text


async def test_free_search_retries_once_on_empty_answer(monkeypatch):
    import ddgs

    from leadhunter.analysis import search

    answers = [[], [{"href": "https://kabluk.ru/"}]]

    class FakeDDGS:
        def text(self, query, **kw):
            return answers.pop(0)

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    monkeypatch.setattr(search, "RETRY_PAUSE", 0)
    assert await search.search_urls("ремонт обуви Тула") == ("бесплатный поиск", ["https://kabluk.ru/"])
    assert answers == []


async def test_empty_search_is_a_note_with_advice(db):
    async def empty(query, key):
        return "бесплатный поиск", []

    d = await service(db, search=empty).build(await save(db, company_row_lead()))
    assert d["seo"] is None and any("Поиск ничего не вернул" in n and "Serper" in n for n in d["notes"])


# --- источники и база ---
async def test_map_leads_carry_meta_for_dossier(db):
    items = parse_yandex_items(FIXTURE)
    with_site = next(i for i in items if i.get("urls"))
    lead = audit_lead(with_site, "sneaknfresh.ru", "без HTTPS", "Тула")
    assert lead.extra["meta"] == {"category": "Химчистка", "city": "Тула", "address": with_site["address"],
                                  "site": "sneaknfresh.ru", "rating": 5, "reviews": 291}
    no_site = next(i for i in items if not i.get("urls"))
    assert "site" not in business_lead(no_site, "ремонт обуви", "Тула").extra["meta"]
    row = await db.get_lead(await save(db, lead))
    assert json.loads(row["meta"])["site"] == "sneaknfresh.ru"
    row = await db.get_lead(await save(db, Lead(source="kwork", external_id="1", text="x")))
    assert row["meta"] is None


# --- бот ---
@pytest.fixture
async def env(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    session = FakeSession()
    bot = Bot("1:TEST", session=session)
    notifier = TelegramNotifier(bot, database, [], hot_score=8)
    pipeline = Pipeline(database, notifier, default_threshold=5)
    guard = Guard(bot, database, [])
    deps = BotDeps(database, [], guard, PhraseProvider(database, ["нужен сайт"]), Workspace(bot, database), None, "профиль", 0, 24)
    deps.extra["dossier"] = service(database, llm=FakeLLM())
    dp = Dispatcher()
    for router in (build_router(deps), build_callbacks(deps), build_dossier_router(deps)):
        dp.include_router(router)
    e = Env(bot, dp, session, database, notifier, pipeline)
    e.guard, e.deps, e.svc = guard, deps, deps.extra["dossier"]
    guard.new_claim_code()
    yield e
    await database.close()
    await session.close()


async def finish(svc, lead_id):
    task = svc._tasks.get(lead_id)
    if task:
        await task


def test_dossier_button_is_under_every_card():
    for kb in (new_lead_keyboard(7), new_lead_keyboard(7, has_draft=True, chat_key="c"), taken_keyboard(7), taken_keyboard(7, True)):
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "ds:7" in data and all(row for row in kb.inline_keyboard)


async def test_button_builds_dossier_and_replies_to_card(env):
    await become_owner(env)
    lead_id = await save(env.db, company_row_lead())
    calls = await env.tap(f"ds:{lead_id}")
    assert ("AnswerCallbackQuery", {"callback_query_id": "cq1", "text": "Собираю досье — пришлю ответом, обычно до минуты."}) in calls
    await finish(env.svc, lead_id)
    sent = [d for n, d in env.session.calls if n == "SendMessage"]
    assert len(sent) == 1 and "Досье на лид #1" in sent[0]["text"] and sent[0]["reply_parameters"]["message_id"] == 5
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"ds:{lead_id}:1"
    # второй раз — готовое, без новых запросов; «Обновить» — пересборка
    asked = len(env.svc.web.asked)
    calls = await env.tap(f"ds:{lead_id}")
    assert any(n == "SendMessage" and "Досье на лид #1" in d["text"] for n, d in calls) and len(env.svc.web.asked) == asked
    await env.tap(f"ds:{lead_id}:1")
    await finish(env.svc, lead_id)
    assert len(env.svc.web.asked) > asked


async def test_dossier_error_is_told_in_plain_words(env):
    await become_owner(env)
    env.deps.extra["dossier"] = env.svc = service(env.db)  # ИИ нет
    lead_id = await save(env.db, Lead(source="kwork", external_id="1", title="Нужен бот", text="Без ссылок"))
    await env.tap(f"ds:{lead_id}")
    await finish(env.svc, lead_id)
    assert any("досье собрать не из чего" in t for t in env.session.sent_texts())


async def test_dossier_is_for_team_only_and_command_works(env):
    await become_owner(env)
    lead_id = await save(env.db, company_row_lead())
    calls = await env.tap(f"ds:{lead_id}", user_id=OTHER)
    assert ("AnswerCallbackQuery", {"callback_query_id": "cq1", "text": "Нет доступа"}) in calls and not env.svc.web.asked
    assert await env.cmd(f"/dossier {lead_id}", user_id=OTHER) == []
    assert "Формат: /dossier 12" in (await env.cmd("/dossier"))[0]
    assert (await env.cmd("/dossier 999"))[0] == "Лид не найден"
    assert (await env.cmd(f"/dossier #{lead_id}"))[0].startswith("Собираю досье")
    await finish(env.svc, lead_id)
    assert any("Досье на лид" in t for t in env.session.sent_texts())
    assert "/dossier N" in (await env.cmd("/help"))[0]


async def test_dossier_is_not_shown_in_foreign_group(env):
    await become_owner(env)
    lead_id = await save(env.db, company_row_lead())
    await env.db.save_dossier(lead_id, {"title": "Каблук", "notes": ["готовое досье"]})
    texts = await env.cmd(f"/dossier {lead_id}", chat_id=-100555)
    assert texts == ["Досье показываю только в личке и в рабочей группе."] and not env.svc.web.asked
    await env.db.set_setting("workspace_chat_id", "-100777")
    assert any("готовое досье" in t for t in await env.cmd(f"/dossier {lead_id}", chat_id=-100777))
    assert any("готовое досье" in t for t in await env.cmd(f"/dossier {lead_id}"))  # личка


# --- автозапуск для горячих лидов ---
async def test_hot_lead_with_site_gets_dossier_automatically(env):
    await become_owner(env)
    env.pipeline.on_delivered = auto_dossier(env.svc, env.db, env.bot, 8)
    hot = Lead(source="kwork", external_id="1", title="Нужен сайт", text="Переделать сайт https://kabluk.ru срочно", preset_score=9)
    env.session.reset()
    assert await env.pipeline.handle(hot)
    await finish(env.svc, 1)
    sent = [d for n, d in env.session.calls if n == "SendMessage"]
    row = await env.db.get_lead(1)
    assert len(sent) == 2 and "Досье на лид #1" in sent[1]["text"]
    assert sent[1]["chat_id"] == row["msg_chat_id"] and sent[1]["reply_parameters"]["message_id"] == row["msg_id"]


@pytest.mark.parametrize("case", ["cold", "no_site", "switched_off", "paused"])
async def test_auto_dossier_is_skipped(env, case):
    await become_owner(env)
    env.pipeline.on_delivered = auto_dossier(env.svc, env.db, env.bot, 8)
    text = "Нужен бот для записи" if case == "no_site" else "Переделать сайт https://kabluk.ru"
    if case == "switched_off":
        await env.db.set_setting("dossier_auto", "0")
    if case == "paused":
        await env.db.set_setting("paused", "1")
    lead = Lead(source="kwork", external_id="1", title="Нужен сайт", text=text, preset_score=6 if case == "cold" else 9)
    assert await env.pipeline.handle(lead)
    assert not env.svc._tasks and await env.db.get_dossier(1) is None and not env.svc.web.asked


async def test_broken_hook_does_not_lose_the_lead(env):
    await become_owner(env)

    async def boom(lead_id, score):
        raise RuntimeError("x")

    env.pipeline.on_delivered = boom
    assert await env.pipeline.handle(Lead(source="kwork", external_id="1", title="Нужен сайт", text="сайт", preset_score=9))
    assert (await env.db.get_lead(1))["msg_id"]


# --- кабинет ---
TOKEN = "123456:TEST-TOKEN"


def headers(user_id=777):
    return {"X-Init-Data": sign_init_data({"auth_date": int(time.time()), "query_id": "q",
                                           "user": {"id": user_id, "first_name": "Стас"}}, TOKEN)}


@pytest.fixture
async def client(db):
    guard = Guard(None, db, [777])
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), Workspace(None, db), None, "профиль")
    deps.extra["threshold"] = 5
    deps.extra["dossier"] = service(db, llm=FakeLLM())
    async with TestClient(TestServer(build_app(MiniAppAPI(db, TOKEN, guard, deps, [])))) as c:
        c.svc = deps.extra["dossier"]
        yield c


async def test_cabinet_builds_and_shows_dossier(client, db):
    lead_id = await save(db, company_row_lead())
    url = f"/api/leads/{lead_id}/dossier"
    assert (await client.get(url)).status == 401 and (await client.post(url, json={})).status == 401
    assert (await client.get(url, headers=headers(999))).status == 403
    assert await (await client.get(url, headers=headers())).json() == {"dossier": None, "running": False, "error": ""}
    r = await client.post(url, json={}, headers=headers())
    assert r.status == 200 and (await r.json()) == {"running": True}
    await finish(client.svc, lead_id)
    data = await (await client.get(url, headers=headers())).json()
    assert not data["running"] and data["dossier"]["site"]["domain"] == "kabluk.ru" and data["dossier"]["ai"]["message"]
    assert (await client.post("/api/leads/999/dossier", json={}, headers=headers())).status == 404
    assert (await client.post(url, json={"force": "да"}, headers=headers())).status == 400


async def test_cabinet_shows_why_dossier_failed(client, db):
    client.svc.get_llm = lambda: None
    lead_id = await save(db, Lead(source="kwork", external_id="1", title="Нужен бот", text="Без ссылок"))
    await client.post(f"/api/leads/{lead_id}/dossier", json={}, headers=headers())
    await finish(client.svc, lead_id)
    data = await (await client.get(f"/api/leads/{lead_id}/dossier", headers=headers())).json()
    assert data["dossier"] is None and "досье собрать не из чего" in data["error"]


async def test_auto_dossier_switch_in_cabinet(client, db):
    assert (await (await client.get("/api/settings", headers=headers())).json())["dossier_auto"] is True
    assert (await client.post("/api/settings", json={"dossier_auto": False}, headers=headers())).status == 200
    assert await db.get_setting("dossier_auto") == "0"
    page = await (await client.get("/app")).text()
    assert 'data-feature="dossier_auto"' in page
