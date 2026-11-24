"""Бриф исполнителя: поля и проверка, отсев по минимальному бюджету, бриф в промптах ИИ,
«почему подходит» в карточке, раздел в кабинете, /brief в боте и «Заполнить за меня»."""
import asyncio
import json

import pytest

from leadhunter import brief
from leadhunter.bot.notify import format_lead
from leadhunter.db import Database
from leadhunter.extract import budget_rub
from leadhunter.llm import LLMClassifier, Verdict, parse_rub
from leadhunter.models import Lead
from leadhunter.pipeline import Enriched, Pipeline
from leadhunter.scoring import ScoreResult
from test_bot_security import GROUP, make_env
from test_miniapp_hardening import MEMBER, auth, make_world

FILLED = {"who": "Стас, один", "services": "Лендинги и Telegram-боты", "prices": "Лендинг от 25 000 ₽",
          "min_budget": "10000", "cases": "Сайт стоматологии: 40 заявок в месяц", "tone": "На «вы», коротко",
          "stop": "Казино", "contact": "@stas"}


class Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, lead_id, lead, info):
        self.sent.append((lead, info))
        return True


class FakeLLM:
    """ИИ-оценщик: запоминает, какой бриф ему передали."""

    def __init__(self):
        self.profiles = []

    async def classify(self, text, profile, good=(), bad=()):
        self.profiles.append(profile)
        return Verdict(True, 8, service="сайт", summary="Салон хочет сайт", reply_draft="Здравствуйте! Сделаю.",
                       why="Сайт — ваша услуга, бюджет выше минимального")


class FillLLM:
    """ИИ для «Заполнить за меня»."""

    def __init__(self, answer=None):
        self.asked = []
        self.answer = answer if answer is not None else {
            "who": "Студия «Ромашка»", "services": ["сайты", "боты"], "prices": "сайт от 30 000 ₽",
            "min_budget": "от десяти тысяч", "client": None, "contact": "@romashka"}

    async def complete_json(self, system, user, **kw):
        self.asked.append((system, user))
        return self.answer


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "brief.db"))
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
async def w(tmp_path):
    world = await make_world(tmp_path)
    yield world
    await world.client.close()
    await world.db.close()


@pytest.fixture
async def env(tmp_path):
    e = await make_env(tmp_path)
    yield e
    await e.db.close()


def lead(text, ext="1", **kw):
    return Lead(source="kwork", external_id=ext, title="Нужен сайт для салона", text=text, intent_implied=True, **kw)


# ---------- сумма заказа в рублях ----------

@pytest.mark.parametrize("text, rub", [
    # заказчик назвал бюджет в рублях
    ("бюджет 50 000 руб", 50000), ("Бюджет: до 15000 ₽", 15000), ("Гонорар: 5 000 руб", 5000), ("оплата 30 тыс. руб", 30000),
    ("бюджет 30к руб", 30000), ("цена вопроса — 3 т.р.", 3000), ("готов заплатить 3000 руб", 3000), ("заплачу 15 тыс руб", 15000),
    # несколько сумм — наибольшая
    ("бюджет 5 000 ₽ — 20 000 ₽", 20000), ("бюджет от 5 000 до 20 000 руб", 20000),
    ("аванс 3000 руб, всего бюджет 15 000 руб", 15000), ("бюджет 30 000 руб за весь проект, аванс 3000 руб", 30000),
    ("бюджет 15 000 руб за сайт", 15000), ("бюджет 12 000 руб, дальше оплата 500 руб за пост", 12000),
    ("бюджет 5000 руб или 30 тыс. рублей в месяц", 5000),
    # сумму никто не назвал бюджетом
    ("за 3 т.р.", None), ("У меня есть сайт за 3000 руб, нужен новый", None), ("5 000 ₽ — 20 000 ₽", None), ("аванс 3000 руб", None),
    # число без рублей: рубли это, доллары или просмотры — неизвестно
    ("бюджет 30к", None), ("бюджет 5к или 10к", None), ("платим за 10к просмотров", None), ("заплачу 5к $", None),
    ("оплата 3000₽, аудитория 30к подписчиков", None), ("Бот для канала с 3 тыс. подписчиков, бюджет 30 000 руб", None),
    ("бюджет 1.5 млн", None), ("на платформе 3к подписчиков", None),
    # валюта, ставка, нижняя граница, нет суммы
    ("бюджет 5000 руб или 100$ за настройку рекламы", None), ("бюджет 20 000 руб за проект или 30$/час", None), ("100$", None),
    ("оплата 500 руб/час", None), ("оплата 500 р. в час", None), ("оплата 2000 рублей за пост", None),
    ("оплата 30 тыс. рублей в месяц", None), ("бюджет от 5000 руб", None), ("нужен сайт", None),
])
def test_budget_in_rubles(text, rub):
    assert budget_rub(text) == rub


# ---------- поля брифа ----------

def test_clean_rejects_unknown_fields_and_bad_minimum():
    assert brief.clean({"min_budget": "10 000 ₽", "who": "  Стас  "}) == {"min_budget": "10000", "who": "Стас"}
    assert brief.clean({"min_budget": "нет"}) == {"min_budget": ""} and brief.clean({"min_budget": 0}) == {"min_budget": ""}
    assert len(brief.clean({"who": "я" * 5000})["who"]) == brief.BY_KEY["who"].limit
    for bad in ({"min_budget": "от десяти тысяч"}, {"hack": "x"}, {"who": ["список"]}, {"who": True}):
        with pytest.raises(brief.BriefError):
            brief.clean(bad)


async def test_save_merges_fields_and_empty_string_clears(db):
    await brief.save(db, {"who": "Стас", "services": "Сайты"})
    assert (await brief.save(db, {"services": "", "prices": "от 25 000 ₽"}))["who"] == "Стас"
    data = await brief.load(db)
    assert data["services"] == "" and data["prices"] == "от 25 000 ₽" and brief.filled(data) == 2
    assert set(data) == set(brief.KEYS)


async def test_two_fields_saved_at_once_both_survive(db):
    # кабинет сохраняет каждое поле отдельным запросом: быстрая правка двух полей не должна затирать первую
    await asyncio.gather(*(brief.save(db, {key: f"значение {key}"}) for key in ("who", "services", "prices", "cases")))
    data = await brief.load(db)
    assert [data[k] for k in ("who", "services", "prices", "cases")] == [
        "значение who", "значение services", "значение prices", "значение cases"]


async def test_text_for_ai_falls_back_to_old_profile_until_brief_is_filled(db):
    assert await brief.profile(db, "Студия: сайты и боты") == "Студия: сайты и боты"
    await db.set_setting("profile", "Делаю сайты на Tilda")
    assert await brief.profile(db, "Студия: сайты и боты") == "Делаю сайты на Tilda"
    await brief.save(db, {"who": "Стас", "min_budget": "10000"})
    text = await brief.profile(db, "Студия: сайты и боты")
    # услуги в брифе не названы — вместо них остаётся старое описание, а не пустота
    assert text.splitlines() == ["Кто вы: Стас", "Услуги: Делаю сайты на Tilda",
                                 "Минимальный заказ: 10000 ₽ — заказы дешевле не берём"]
    await brief.save(db, FILLED)
    text = await brief.profile(db, "Студия: сайты и боты")
    assert "Услуги: Лендинги и Telegram-боты" in text and "Tilda" not in text and "Кейсы: Сайт стоматологии" in text


async def test_broken_saved_brief_does_not_crash(db):
    await db.set_setting("brief", "{не json")
    assert brief.filled(await brief.load(db)) == 0
    await db.set_setting("brief", "[1, 2]")
    assert await brief.profile(db, "запасной") == "запасной"


# ---------- отсев и отклик ----------

async def test_order_below_minimum_budget_is_not_delivered(db):
    notifier = Notifier()
    p = Pipeline(db, notifier, default_threshold=1)
    assert await p.handle(lead("Нужен сайт, бюджет 3000 руб", "0"))  # брифа нет — приходит
    await brief.save(db, {"min_budget": "10000"})
    assert not await p.handle(lead("Нужен лендинг для салона красоты, бюджет 3000 руб", "1"))
    assert await p.handle(lead("Нужен интернет-магазин одежды под ключ, бюджет 40 000 руб", "2"))
    assert await p.handle(lead("Ищу кто сделает сайт-визитку автосервису, бюджет обсудим", "3"))  # сумма не названа
    assert await p.handle(lead("Сделать квиз на сайт стоматологии, оплата 800 руб/час", "4"))  # ставка, не бюджет
    assert await p.handle(lead("Нужен бот для телеграм-канала на 3 тыс. подписчиков, бюджет 30 000 руб", "5"))
    assert await p.handle(lead("Нужен корпоративный сайт под ключ, вилка 5 000 ₽ — 20 000 ₽", "6"))
    assert len(notifier.sent) == 6


async def test_owner_rule_beats_minimum_budget(db):
    notifier = Notifier()
    await brief.save(db, {"min_budget": "10000"})
    await db.add_rule("+визитка")
    assert await Pipeline(db, notifier, default_threshold=1).handle(lead("Нужна визитка для мастера, бюджет 3000 руб"))


class BudgetLLM(FakeLLM):
    """Как настоящий ИИ: называет сумму заказа числом, а лид это или нет — решает только по теме."""

    def __init__(self, rub):
        super().__init__()
        self.rub = rub

    async def classify(self, text, profile, good=(), bad=()):
        verdict = await super().classify(text, profile, good, bad)
        verdict.budget_rub = self.rub
        return verdict


async def test_minimum_is_compared_by_program_with_the_sum_named_by_ai(db):
    await brief.save(db, FILLED)
    notifier, cheap = Notifier(), BudgetLLM(3000)
    p = Pipeline(db, notifier, llm=cheap, default_threshold=1)
    # «тысячи три» числом не разобрать — сумму называет ИИ, с минимумом 10 000 ₽ её сравнивает программа
    assert not await p.handle(lead("Нужен сайт для кафе, бюджет небольшой, тысячи три", "1"))
    # сам минимум ИИ не видит: иначе он отклоняет заказы по своему разумению, а не по брифу
    assert "Минимальный заказ" not in cheap.profiles[0] and "Кейсы: Сайт стоматологии" in cheap.profiles[0]
    for ext, rub, text in (("2", 15000, "Нужен интернет-магазин одежды, бюджет тысяч пятнадцать"),
                           ("3", 10000, "Ищу кто сделает сайт-визитку автосервису, бюджет десятка"),
                           ("4", None, "Сделать квиз на сайт стоматологии, бюджет обсудим")):
        assert await Pipeline(db, notifier, llm=BudgetLLM(rub), default_threshold=1).handle(lead(text, ext))
    assert len(notifier.sent) == 3


async def test_sum_named_by_ai_does_not_filter_without_minimum_in_brief(db):
    notifier = Notifier()
    await brief.save(db, {**FILLED, "min_budget": "0"})
    assert await Pipeline(db, notifier, llm=BudgetLLM(500), default_threshold=1).handle(
        lead("Нужен сайт для кафе, бюджет небольшой", "1"))


async def test_owner_rule_beats_minimum_checked_after_ai(db):
    notifier, llm = Notifier(), BudgetLLM(3000)
    await brief.save(db, FILLED)
    await db.add_rule("+визитка")
    assert await Pipeline(db, notifier, llm=llm, default_threshold=1).handle(
        lead("Нужна визитка для мастера, бюджет тысячи три", "1"))


@pytest.mark.parametrize("raw, rub", [
    (15000, 15000), (15000.0, 15000), ("15000", 15000), ("15 000", 15000), ("до 15\u00a0000 ₽", 15000),
    (None, None), ("null", None), ("", None), ("не назван", None), (0, None), (-5, None), (True, None),
    ([], None), (10 ** 12, None),
])
def test_ai_budget_in_rubles_is_parsed_as_number(raw, rub):
    assert parse_rub(raw) == rub


async def test_classifier_prompt_leaves_budget_threshold_to_program():
    class Client(LLMClassifier):
        async def complete_json(self, system, user, **kw):
            self.system = system
            return {"is_lead": True, "score": 4, "budget": "до 15 000 ₽", "budget_rub": "15 000", "why": "Сайт"}

    client = Client("k", "http://x", "m")
    verdict = await client.classify("Нужен сайт, бюджет до 15 000 ₽", "Услуги: сайты")
    await client.client.aclose()
    assert verdict.is_lead and verdict.budget_rub == 15000 and verdict.budget == "до 15 000 ₽"
    assert "Бюджет на is_lead не влияет" in client.system and '"budget_rub"' in client.system
    assert "назван только снизу («от …»)" in client.system  # «от 5 000 ₽» — не потолок, отсеивать по нему нельзя
    # причина раньше решения: модель сначала объясняет, потом отвечает «лид или нет»
    assert client.system.index('"why"') < client.system.index('"is_lead"')
    # отклик: контакт и род не выдумываются, дешёвому заказчику не отказывают
    for rule in ("если контакта в брифе нет, никакого не называй", "без рода", "не отказывай, не поучай",
                 "нет такой цены в брифе — никаких сумм не называй"):
        assert rule in client.system


async def test_ai_gets_brief_and_card_shows_why_it_fits(db):
    notifier, llm = Notifier(), FakeLLM()
    await brief.save(db, FILLED)
    assert await Pipeline(db, notifier, llm=llm, default_threshold=1, profile="старый профиль").handle(
        lead("Нужен сайт для салона красоты, бюджет 40 000 руб"))
    assert "Кейсы: Сайт стоматологии" in llm.profiles[0] and "старый профиль" not in llm.profiles[0]
    row = await db.get_lead(1)
    assert row["why"] == "Сайт — ваша услуга, бюджет выше минимального" and row["draft"] == "Здравствуйте! Сделаю."
    sent_lead, info = notifier.sent[0]
    assert "✅ Почему вам подходит: Сайт — ваша услуга" in format_lead(1, sent_lead, info)


def test_card_escapes_why_and_omits_it_when_empty():
    res = ScoreResult(5, [], [], [])
    assert "Почему" not in format_lead(1, lead("текст заказа"), Enriched(5, res))
    card = format_lead(1, lead("текст заказа"), Enriched(5, res, why="бюджет <b>40к</b> & ваш город"))
    assert "бюджет &lt;b&gt;40к&lt;/b&gt; &amp; ваш город" in card


async def test_classifier_prompt_carries_brief_and_reads_why():
    llm = LLMClassifier("k", "http://127.0.0.1:1", "m")
    seen = {}

    async def complete_json(system, user, temperature=0):
        seen["system"] = system
        return {"is_lead": True, "score": 9, "summary": "с", "why": "ваш кейс в той же нише", "reply_draft": "Здравствуйте"}

    llm.complete_json = complete_json
    try:
        verdict = await llm.classify("нужен сайт", brief.text(FILLED))
    finally:
        await llm.client.aclose()
    assert verdict.why == "ваш кейс в той же нише"
    assert "Цены: Лендинг от 25 000 ₽" in seen["system"] and "Что не берёте: Казино" in seen["system"]
    assert "не выдумывай" in seen["system"]


async def test_old_database_gets_why_column(tmp_path):
    path = str(tmp_path / "old.db")
    d = Database(path)
    await d.connect()
    await d.conn.execute("ALTER TABLE leads DROP COLUMN why")
    await d.conn.commit()
    await d.close()
    d = Database(path)
    await d.connect()
    try:
        lead_id = await d.save_lead(lead("текст заказа"), 5, ScoreResult(5, [], [], []), why="подходит")
        assert (await d.get_lead(lead_id))["why"] == "подходит"
    finally:
        await d.close()


async def test_content_prompt_uses_brief(w):
    await brief.save(w.db, FILLED)
    system = await w.factory.gen._system("post")
    assert "Кейсы: Сайт стоматологии" in system and "Тон общения: На «вы», коротко" in system


# ---------- кабинет ----------

async def test_cabinet_brief_owner_saves_member_only_reads(w):
    r = await (await w.client.get("/api/brief", headers=auth())).json()
    assert [f["key"] for f in r["fields"]] == list(brief.KEYS) and r["filled"] == 0 and r["can_edit"] and not r["ai"]
    r = await w.client.post("/api/brief", headers=auth(), json={"who": "Стас", "min_budget": "15 000"})
    body = await r.json()
    assert r.status == 200 and body["brief"]["min_budget"] == "15000" and body["filled"] == 2
    settings = await (await w.client.get("/api/settings", headers=auth())).json()
    assert settings["brief_filled"] == 2 and settings["brief_total"] == len(brief.FIELDS)
    # участник команды видит бриф, но не меняет: от имени владельца ИИ пишет клиентам
    seen = await (await w.client.get("/api/brief", headers=auth(MEMBER, "Маша"))).json()
    assert seen["brief"]["who"] == "Стас" and not seen["can_edit"]
    assert (await w.client.post("/api/brief", headers=auth(MEMBER, "Маша"), json={"who": "Маша"})).status == 403
    assert (await brief.load(w.db))["who"] == "Стас"


async def test_cabinet_brief_rejects_bad_input_without_saving_anything(w):
    for body in ({"who": "Стас", "min_budget": "много"}, {"who": "Стас", "hack": "x"}, {"who": 5.5}, ["who"]):
        r = await w.client.post("/api/brief", headers=auth(), json=body)
        assert r.status == 400, body
    assert brief.filled(await brief.load(w.db)) == 0
    assert (await w.client.get("/api/brief")).status in (401, 403)


async def test_cabinet_fill_for_me_suggests_fields_and_saves_nothing(w):
    r = await w.client.post("/api/brief/fill", headers=auth(), json={"source": "Мы студия «Ромашка», делаем сайты и ботов"})
    assert r.status == 400 and "ИИ не подключён" in (await r.json())["error"]
    w.deps.llm = FillLLM()
    assert (await w.client.post("/api/brief/fill", headers=auth(MEMBER, "Маша"), json={"source": "x" * 50})).status == 403
    assert (await w.client.post("/api/brief/fill", headers=auth(), json={"source": "мало"})).status == 400
    r = await w.client.post("/api/brief/fill", headers=auth(), json={"source": "Мы студия «Ромашка», делаем сайты и ботов"})
    found = (await r.json())["brief"]
    assert r.status == 200 and found["who"] == "Студия «Ромашка»" and found["services"] == "сайты; боты"
    # «от десяти тысяч» — не число: поле остаётся пустым, человек впишет сам
    assert found["min_budget"] == "" and found["client"] == "" and set(found) == set(brief.KEYS)
    assert brief.filled(await brief.load(w.db)) == 0
    assert len(w.deps.llm.asked) == 1 and "ничего не выдумывай" in w.deps.llm.asked[0][0]


async def test_cabinet_fill_reports_ai_failure_in_plain_words(w):
    class Broken:
        async def complete_json(self, *a, **kw):
            raise asyncio.TimeoutError()

    w.deps.llm = Broken()
    r = await w.client.post("/api/brief/fill", headers=auth(), json={"source": "Мы студия «Ромашка», делаем сайты и ботов"})
    assert r.status == 400 and "не ответил вовремя" in (await r.json())["error"]


async def test_cabinet_lead_shows_why(w):
    lead_id = await w.db.save_lead(lead("текст заказа", "77"), 7, ScoreResult(7, [], [], []), why="ваша услуга и ваш город")
    r = await (await w.client.get(f"/api/leads/{lead_id}", headers=auth())).json()
    assert r["lead"]["why"] == "ваша услуга и ваш город"


def test_cabinet_page_has_brief_screen():
    from leadhunter.miniapp.server import STATIC
    page = open(f"{STATIC}/index.html", encoding="utf-8").read()
    js = open(f"{STATIC}/app.js", encoding="utf-8").read()
    assert 'id="screen-brief"' in page and 'id="open-brief"' in page and 'id="brief-fill-go"' in page
    assert "/api/brief/fill" in js and "l.why" in js


# ---------- сайт для «Заполнить за меня» ----------

def test_link_or_text():
    for url in ("example.ru", "https://example.ru/about", "мой-сайт.рф", "http://site.com:8080/x", "http://127.0.0.1:8080/admin"):
        assert brief.looks_like_url(url), url
    for text in ("Мы студия, делаем сайты. Наш сайт example.ru", "сайты и боты", ""):
        assert not brief.looks_like_url(text), text


def test_page_text_drops_scripts_and_tags():
    page = "<html><head><style>p{}</style><script>alert('x')</script></head><body><h1>Студия &laquo;Ромашка&raquo;</h1><p>Сайты\n от 30&nbsp;000</p></body>"
    assert brief.html_to_text(page) == "Студия «Ромашка» Сайты от 30\xa0000".replace("\xa0", " ")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/", "http://localhost/admin", "http://169.254.169.254/latest/meta-data",
                                 "http://[::1]/", "http://10.0.0.5/", "http://192.168.1.1/", "http://0.0.0.0/",
                                 "ftp://example.com/x", "http://user:pass@example.com/", "https:///nohost"])
async def test_site_link_to_internal_address_is_refused(url, monkeypatch):
    import curl_cffi.requests

    def no_request(*a, **kw):
        raise AssertionError("запрос к внутреннему адресу")

    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", no_request)
    with pytest.raises(brief.BriefError):
        await brief.site_text(url)


class FakeSite:
    """Вместо curl: отдаёт заранее заданные ответы и запоминает, куда и с каким закреплённым адресом ходили."""

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def session(self, **kw):
        site = self

        class Response:
            def __init__(self, status, headers, body):
                self.status_code, self.headers, self.body, self.encoding = status, headers, body, "utf-8"

            async def aiter_content(self):
                yield self.body

            async def aclose(self):
                pass

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **opts):
                assert not kw.get("proxy") and not kw.get("proxies")  # через прокси закрепить проверенный адрес нельзя
                site.requests.append((url, list(kw["curl_options"].values())[0], opts.get("allow_redirects")))
                return Response(*site.responses.pop(0))

        return Session()


@pytest.fixture
def dns(monkeypatch):
    """example.ru — адрес в интернете; evil.example — внутренний адрес."""
    table = {"example.ru": "93.184.216.34", "www.example.ru": "93.184.216.34", "evil.example": "10.0.0.7"}

    async def resolve(host, port):
        if host not in table:
            raise OSError("нет такого имени")
        return [table[host]]

    monkeypatch.setattr(brief, "_resolve", resolve)


async def test_site_text_follows_redirect_and_pins_checked_address(dns, monkeypatch):
    import curl_cffi.requests

    page = ("<html><body><h1>Студия Ромашка</h1><p>" + "Делаем сайты и ботов для малого бизнеса. " * 10 + "</p></body></html>").encode()
    site = FakeSite([(301, {"location": "https://www.example.ru/"}, b""), (200, {}, page)])
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", site.session)
    text = await brief.site_text("example.ru")
    assert text.startswith("Студия Ромашка Делаем сайты")
    assert site.requests == [("https://example.ru", ["example.ru:443:93.184.216.34"], False),
                             ("https://www.example.ru/", ["www.example.ru:443:93.184.216.34"], False)]


async def test_site_redirect_to_internal_address_is_refused(dns, monkeypatch):
    import curl_cffi.requests

    site = FakeSite([(302, {"location": "http://evil.example/secret"}, b""), (200, {}, b"secret " * 100)])
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", site.session)
    with pytest.raises(brief.BriefError, match="не из интернета"):
        await brief.site_text("https://example.ru/")
    assert len(site.requests) == 1  # к внутреннему адресу запрос не ушёл


async def test_site_errors_are_plain_words(dns, monkeypatch):
    import curl_cffi.requests

    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", FakeSite([(404, {}, b"")]).session)
    with pytest.raises(brief.BriefError, match="ошибку 404"):
        await brief.site_text("example.ru")
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", FakeSite([(200, {}, "<html>пусто</html>".encode())]).session)
    with pytest.raises(brief.BriefError, match="почти нет текста"):
        await brief.site_text("example.ru")
    with pytest.raises(brief.BriefError, match="не найден"):
        await brief.site_text("no-such-site.example")


async def test_site_is_fetched_directly_even_when_proxy_is_configured(dns, monkeypatch, env, w):
    import curl_cffi.requests

    page = ("<p>" + "Студия Ромашка: сайты и боты для малого бизнеса. " * 10 + "</p>").encode()
    site = FakeSite([(200, {}, page), (200, {}, page)])
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", site.session)
    env.settings.proxy_url = "http://proxy.example:3128"  # прокси для бирж
    w.deps.extra["settings"] = env.settings
    w.deps.llm = env.deps.llm = FillLLM()
    r = await w.client.post("/api/brief/fill", headers=auth(), json={"source": "https://example.ru"})
    assert r.status == 200, await r.text()
    await env.tap("br:fill")
    assert "Вот что я понял" in await env.say("https://example.ru")
    assert [pin for _, pin, _ in site.requests] == [["example.ru:443:93.184.216.34"]] * 2


async def test_fill_from_link_reads_site_then_asks_ai(dns, monkeypatch):
    import curl_cffi.requests

    page = ("<p>" + "Студия Ромашка: сайты и боты для малого бизнеса. " * 10 + "</p>").encode()
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", FakeSite([(200, {}, page)]).session)
    llm = FillLLM()
    found = await brief.fill_from(llm, "https://example.ru")
    assert found["contact"] == "@romashka" and "Студия Ромашка: сайты и боты" in llm.asked[0][1]


# ---------- бот ----------

def buttons(env) -> list[str]:
    """callback_data всех кнопок в отправленных сообщениях."""
    out = []
    for d in env.named("SendMessage", "EditMessageText"):
        for row in (d.get("reply_markup") or {}).get("inline_keyboard", []):
            out += [b.get("callback_data") for b in row]
    return out


async def test_bot_brief_questions_one_by_one(env):
    said = await env.say("/brief")
    assert "заполнено 0 из 11" in said and "br:q:who" in buttons(env) and "br:fill" not in buttons(env)  # ИИ не подключён
    await env.tap("br:q:who")
    assert "1/11 · Кто вы" in env.said() and "br:s:who" in buttons(env)
    assert "2/11 · Услуги" in await env.say("Стас, делаю сайты один")
    assert (await brief.load(env.db))["who"] == "Стас, делаю сайты один"
    await env.tap("br:s:services")  # пропустить
    assert "3/11 · Цены" in env.said()
    assert "4/11 · Минимальный заказ" in await env.say("Лендинг от 25 000 ₽")
    assert "Не принято" in await env.say("тысяч десять")
    assert "5/11 · Идеальный клиент" in await env.say("10 000")
    await env.tap("br:done")
    data = await brief.load(env.db)
    assert data["min_budget"] == "10000" and data["services"] == "" and brief.filled(data) == 3
    assert "заполнено 3 из 11" in env.said() and "br:q:services" in buttons(env)  # «Продолжить» — с первого пустого


async def test_bot_brief_edit_and_clear_one_field(env):
    await brief.save(env.db, FILLED)
    await env.tap("br:e:prices")
    assert "Сейчас: Лендинг от 25 000 ₽" in env.said() and "br:x:prices:0" in buttons(env)
    assert "✅ Сохранил" in await env.say("Лендинг от 30 000 ₽")
    assert (await brief.load(env.db))["prices"] == "Лендинг от 30 000 ₽"
    await env.tap("br:e:stop")
    await env.tap("br:x:stop:0")
    assert (await brief.load(env.db))["stop"] == ""
    # после ответа режим ввода закрыт: следующее сообщение бриф не меняет
    await env.say("просто сообщение")
    assert (await brief.load(env.db))["prices"] == "Лендинг от 30 000 ₽"


async def test_bot_brief_is_edited_only_by_owner_in_private_chat(env):
    await env.db.set_setting("workspace_chat_id", str(GROUP))
    await env.db.add_member(777, "Маша")
    assert await env.press("br:e:who", 777) == ["Только владелец"]
    assert "только в личке" in "".join(await env.press("br:e:who", chat_id=GROUP))
    said = await env.say("/brief", 777)  # участник видит бриф без кнопок правки
    assert "заполнено 0 из 11" in said and buttons(env) == []
    assert "Нет такого поля" in "".join(await env.press("br:e:nope"))
    assert brief.filled(await brief.load(env.db)) == 0


async def test_bot_never_shows_brief_in_a_group(env):
    await brief.save(env.db, FILLED)
    await env.db.set_setting("workspace_chat_id", str(GROUP))
    for chat in (GROUP, -1009999999999):  # рабочая группа и посторонняя
        said = await env.say("/brief", chat_id=chat)
        assert "только в личке" in said and "Лендинг" not in said and "Стас" not in said and buttons(env) == [], chat
        await env.tap("ob:brief", chat_id=chat)
        assert "только в личке" in env.said() and "Лендинг" not in env.said(), chat
    assert "Лендинг от 25 000" in await env.say("/brief")


async def test_bot_fill_for_me_saves_only_after_confirmation(env):
    assert "ИИ не подключён" in "".join(await env.press("br:fill"))
    env.deps.llm = FillLLM()
    await brief.save(env.db, {"stop": "Казино"})
    await env.say("/brief")
    assert "br:fill" in buttons(env)
    await env.tap("br:fill")
    said = await env.say("Мы студия «Ромашка», делаем сайты и ботов, пишите @romashka")
    assert "Вот что я понял" in said and "Студия «Ромашка»" in said and "br:fillok" in buttons(env)
    assert brief.filled(await brief.load(env.db)) == 1  # пока не подтвердили — ничего не сохранено
    await env.tap("br:fillok")
    data = await brief.load(env.db)
    assert data["who"] == "Студия «Ромашка»" and data["contact"] == "@romashka" and data["stop"] == "Казино"
    assert "Предложение устарело" in "".join(await env.press("br:fillok"))  # второй раз сохранять нечего


async def test_bot_fill_for_me_shows_everything_it_is_going_to_save(env):
    long_cases = " ".join(f"Кейс {i}: сайт для клиники, заявок стало больше." for i in range(60))  # ~3000 знаков
    long_edge = "Сдаю в срок. " * 110 + "ХВОСТ-ПРЕИМУЩЕСТВ"
    env.deps.llm = FillLLM({"who": "Студия", "cases": long_cases, "edge": long_edge, "services": "Сайты. " * 200})
    await env.tap("br:fill")
    said = await env.say("Мы студия, у нас много кейсов и преимуществ, вот они все подряд")
    assert "Кейс 59" in said and "ХВОСТ-ПРЕИМУЩЕСТВ" in said  # концы длинных полей тоже показаны
    sent = env.named("SendMessage", "EditMessageText")
    assert len(sent) > 1 and not env.named("SendMessage:REJECTED", "EditMessageText:REJECTED")
    assert [bool(d.get("reply_markup")) for d in sent[1:]] == [False] * (len(sent) - 2) + [True]  # кнопки — под последним
    await env.tap("br:fillok")
    data = await brief.load(env.db)
    assert data["cases"] in said.replace("\n", " ") and data["edge"].endswith("ХВОСТ-ПРЕИМУЩЕСТВ")


async def test_bot_fill_for_me_explains_failures(env):
    env.deps.llm = FillLLM({})
    await env.tap("br:fill")
    assert "не нашлось ничего для брифа" in await env.say("Просто длинный текст ни о чём, без сведений о студии")
    await env.tap("br:fill")
    assert "Не получилось" in await env.say("http://127.0.0.1:8080/admin")
    assert brief.filled(await brief.load(env.db)) == 0


async def test_start_and_profile_point_to_brief(env):
    said = await env.say("/start")
    assert "2️⃣ Бриф — /brief" in said and "ob:brief" in buttons(env)
    await env.tap("ob:brief")
    assert "заполнено 0 из 11" in env.said()
    await brief.save(env.db, FILLED)
    assert "✅ Бриф — /brief" in await env.say("/start")
    assert "ИИ опирается на него" in await env.say("/profile Делаю сайты")
    assert "/brief" in await env.say("/help")
