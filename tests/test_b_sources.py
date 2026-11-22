"""Модуль B, источники: находки аудита №1–5, 7, 14, 15 (docs/audit/01_leads.md). Только фикстуры и моки."""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from leadhunter.config import Settings
from leadhunter.db import Database
from leadhunter.extract import extract_budget
from leadhunter.models import Lead
from leadhunter.phrases import PhraseProvider
from leadhunter.sources import build_sources
from leadhunter.sources.base import Source, strip_html
from leadhunter.sources.freelance_ru import parse_freelance_ru_html
from leadhunter.sources.hh import HHSource
from leadhunter.sources.kwork import parse_kwork_html
from leadhunter.sources.rss_boards import parse_feed
from leadhunter.sources.telegram_chats import TelegramChatsSource
from leadhunter.sources.threads_web import ThreadsWebSource, parse_threads_html
from leadhunter.sources.web_search import WebSearchSource
from leadhunter.sources.weblancer import parse_weblancer_html
from leadhunter.sources.yandex_maps_web import business_lead, parse_yandex_items

FIX = Path(__file__).parent / "fixtures"
REAL_CLIENT = httpx.AsyncClient


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def moscow_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def emit_nothing(lead, quiet=False):
    return False


# --- разбор площадок по сохранённым страницам ---

def test_kwork_fixture_parses():
    leads = parse_kwork_html(fixture("kwork_projects.html"))
    assert len(leads) == 12
    first = next(l for l in leads if l.external_id == "3274956")
    assert first.title == "Допиливание мобильного приложения"
    assert first.competitors is not None and first.found_at.utcoffset() == timedelta(hours=3)
    assert all(l.source == "kwork" and l.intent_implied and l.url.startswith("https://kwork.ru/projects/") for l in leads)


def test_weblancer_fixture_parses():
    leads = parse_weblancer_html(fixture("weblancer_jobs.html"))
    assert len(leads) == 20
    assert all(l.title and l.url.startswith("https://www.weblancer.net/") and l.found_at.tzinfo for l in leads)
    assert any(l.competitors for l in leads)


def test_yandex_maps_fixture_parses():
    items = parse_yandex_items(fixture("yandex_maps_search.html"))
    assert len(items) == 24
    no_site = [x for x in items if not x.get("urls")]
    assert no_site, "в выдаче есть организации без сайта — это и есть лиды"
    lead = business_lead(no_site[0], "ремонт обуви", "Тула")
    assert lead.source == "yandex_maps" and lead.preset_score == 6 and "Тула" in lead.text and lead.title


# --- №1: время FL.ru не зависит от пояса сервера ---

def test_fl_rss_time_is_utc_on_moscow_server(moscow_tz):
    leads = parse_feed((FIX / "fl_ru.xml").read_bytes(), "https://www.fl.ru/rss/all.xml")
    assert len(leads) == 60
    # первый заказ в ленте: <pubDate>Sun, 22 Nov 2026 19:34:40 GMT</pubDate>
    assert leads[0].found_at == datetime(2026, 11, 22, 19, 34, 40, tzinfo=timezone.utc)


# --- №2–3: Freelance.ru — премиум-карточки и сумма ---

def test_freelance_ru_takes_premium_cards():
    body = fixture("freelance_ru_tasks.html")
    assert body.count('<article class="task-card task-card--premium">') == 21
    assert len(parse_freelance_ru_html(body)) == 25


def test_freelance_ru_budget_is_amount_not_label():
    leads = parse_freelance_ru_html(fixture("freelance_ru_tasks.html"))
    assert not any("Гонорар: Гонорар" in l.text for l in leads)
    priced = [l for l in leads if "Гонорар:" in l.text]
    assert priced, "хотя бы у части заказов сумма указана"
    assert any("2 800 ₽" in l.text for l in priced)
    assert all(extract_budget(l.text) for l in priced)


# --- №14: HTML-сущности ---

def test_strip_html_decodes_numeric_entities():
    assert strip_html("Бюджет: 15&nbsp;000 &#8381; &mdash; <b>срочно</b>") == "Бюджет: 15 000 ₽ — срочно"


def test_fl_rss_titles_have_no_raw_entities():
    leads = parse_feed((FIX / "fl_ru.xml").read_bytes(), "https://www.fl.ru/rss/all.xml")
    assert not any("&#" in l.title or "&#" in l.text for l in leads)
    assert leads[0].title == "Автоматизация пабг мобайл (Бюджет: 15 000 ₽)"
    assert extract_budget(leads[0].full_text) == "15 000 ₽"


# --- №4: Threads без логина — пустая/старая выдача не считается «✅» ---

class FixtureThreads(ThreadsWebSource):
    def __init__(self):
        self.interval = 900
        self.polls = 0
        self.alerts = []
        self.alert = self._collect

    async def _collect(self, text):
        self.alerts.append(text)

    async def poll(self):
        self.polls += 1
        return parse_threads_html(fixture("threads_search.html"))


async def test_threads_stale_results_are_flagged_and_alerted_once():
    src = FixtureThreads()
    posts = await src.poll()
    assert len(posts) == 18
    newest = max(p.found_at for p in posts)
    assert newest < datetime(2026, 11, 7, tzinfo=timezone.utc)  # самому свежему посту на момент съёмки 16 дней

    pauses = [await src.step(emit_nothing) for _ in range(5)]
    assert src.alerts and len(src.alerts) == 1, "один алерт на серию, а не на каждый проход"
    assert "threads" in src.alerts[0] and "48 ч" in src.alerts[0]
    line = src.status_line()
    assert "✅" not in line and "⚠️" in line and "без записей моложе 48 ч" in line
    assert pauses[0] == 900 and pauses[-1] > 900, "пустой источник опрашивается реже"


async def test_empty_result_is_flagged_and_fresh_result_clears_it():
    class Empty(Source):
        name = "kwork"
        interval = 600
        rows: list = []

        async def poll(self):
            return list(self.rows)

    src = Empty()
    alerts = []

    async def alert(text):
        alerts.append(text)

    src.alert = alert
    for _ in range(3):
        await src.step(emit_nothing)
    assert "выдача пустая" in src.status_line() and len(alerts) == 1
    src.rows = [Lead(source="kwork", external_id="1", text="Нужен сайт для кофейни")]
    assert await src.step(emit_nothing) == 600
    assert "✅" in src.status_line() and src.last_warning is None


async def test_step_sorts_naive_and_aware_dates():
    class Mixed(Source):
        name = "mixed"

        async def poll(self):
            return [Lead(source="x", external_id="1", text="a", found_at=datetime.now(timezone.utc).replace(tzinfo=None)),
                    Lead(source="x", external_id="2", text="b", found_at=datetime.now(timezone.utc))]

    src = Mixed()
    await src.step(emit_nothing)
    assert src.last_error is None


# --- №5: hh.ru с заглушкой вместо User-Agent не опрашивается; отказ объясняется ---

def settings(**kw) -> Settings:
    return Settings(bot_token="1:TEST", _env_file=None, **kw)


def hh_transport(monkeypatch, status: int, body: dict) -> list:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=body)

    monkeypatch.setattr("leadhunter.sources.hh.httpx.AsyncClient",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return calls


async def test_hh_with_placeholder_user_agent_is_not_polled(monkeypatch):
    calls = hh_transport(monkeypatch, 400, {"errors": [{"type": "bad_user_agent", "value": "blacklisted"}]})
    src = HHSource(settings(hh_user_agent="LeadHunter/0.1 (contact@example.com)"))
    alerts = []

    async def alert(text):
        alerts.append(text)

    src.alert = alert
    for _ in range(4):
        await src.step(emit_nothing)
    assert calls == [] and alerts == []
    assert "HH_USER_AGENT" in src.status_line() and "✅" not in src.status_line()


async def test_hh_rejection_is_explained(monkeypatch):
    hh_transport(monkeypatch, 400, {"errors": [{"type": "bad_user_agent", "value": "blacklisted"}]})
    src = HHSource(settings(hh_user_agent="MyStudio/1.0 (me@studio.ru)"))
    await src.step(emit_nothing)
    assert "HH_USER_AGENT" in src.last_error and "Bad Request" not in src.last_error

    hh_transport(monkeypatch, 403, {"errors": [{"type": "forbidden"}]})
    await src.step(emit_nothing)
    assert "IP" in src.last_error


# --- №15: выключенные группы не опрашиваются; сбой DDG — это сбой, а не пустая выдача ---

async def test_disabled_groups_are_not_polled(db, monkeypatch):
    s = settings(hh_user_agent="MyStudio/1.0 (me@studio.ru)", web_search_sites=["vc.ru"], threads_web_enabled=False,
                 kwork_enabled=False, weblancer_enabled=False, freelance_ru_enabled=False, rss_feeds=[],
                 yandex_web_enabled=False, ig_web_enabled=False)
    sources = {x.name: x for x in build_sources(s, db, PhraseProvider(db, ["нужен сайт"]))}
    assert {"hh.ru", "web"} <= set(sources)
    polled = []
    for name, src in sources.items():
        async def poll(name=name):
            polled.append(name)
            return [Lead(source=name, external_id="1", text="Нужен сайт для кофейни, бюджет 50 000 руб")]
        monkeypatch.setattr(src, "poll", poll)

    for src in sources.values():
        await src.step(emit_nothing)
    assert polled == [], "группы «Вакансии» и «Поисковики» выключены по умолчанию"
    assert "выключен" in sources["web"].status_line() and "не опрашивается" in sources["hh.ru"].status_line()

    await db.set_setting("leads_web", "1")
    for src in sources.values():
        await src.step(emit_nothing)
    assert polled == ["web"]
    assert "выключен" not in sources["web"].status_line()


async def test_enabled_board_group_can_be_switched_off(db, monkeypatch):
    s = settings(threads_web_enabled=False, weblancer_enabled=False, freelance_ru_enabled=False, rss_feeds=[],
                 yandex_web_enabled=False, ig_web_enabled=False, hh_enabled=False, web_search_sites=[], work_hours="0-24")
    (kwork,) = build_sources(s, db, PhraseProvider(db, ["нужен сайт"]))
    polled = []

    async def poll():
        polled.append(1)
        return []

    monkeypatch.setattr(kwork, "poll", poll)
    await kwork.step(emit_nothing)
    await db.set_setting("leads_boards", "0")
    await kwork.step(emit_nothing)
    assert len(polled) == 1


def fake_ddgs(monkeypatch, error: Exception | None, rows=()):
    import ddgs

    class FakeDDGS:
        def text(self, query, **kw):
            if error:
                raise error
            return list(rows)

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)


async def test_ddg_failure_is_an_error_not_empty_result(db, monkeypatch):
    from ddgs.exceptions import DDGSException, RatelimitException

    src = WebSearchSource(settings(), PhraseProvider(db, ["нужен сайт"]), ["vc.ru"])
    fake_ddgs(monkeypatch, DDGSException("No results found."))
    assert await src._ddg('site:vc.ru "нужен сайт"') == []

    fake_ddgs(monkeypatch, RatelimitException("202 Ratelimit"))
    await src.step(emit_nothing)
    assert src.last_error and "DuckDuckGo" in src.last_error
    assert "⚠️" in src.status_line() and "✅" not in src.status_line()


# --- №7: название чата не попадает в текст для оценки ---

def tg_lead(chat_title: str, text: str) -> Lead:
    message = SimpleNamespace(id=7, raw_text=text, date=datetime.now(timezone.utc))
    chat = SimpleNamespace(id=100500, title=chat_title, username="boty_i_saity")
    return TelegramChatsSource._to_lead(None, message, chat, None)


def test_telegram_chat_title_is_not_part_of_scored_text():
    lead = tg_lead("Боты и сайты", "Подскажите, нужен юрист по разводу")
    assert lead.full_text == "Подскажите, нужен юрист по разводу"
    assert lead.extra["chat_title"] == "Боты и сайты" and lead.extra["chat_key"] == "100500"
