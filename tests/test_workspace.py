import pytest

from leadhunter.bot.workspace import route
from leadhunter.db import Database
from leadhunter.models import Lead
from leadhunter.pipeline import Enriched, Pipeline
from leadhunter.scoring import ScoreResult


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, lead_id, lead, info):
        self.sent.append((lead_id, lead, info))


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    yield db
    await db.close()


def lead(ext, text, **kw):
    return Lead(source=kw.pop("source", "telegram"), external_id=ext, text=text, **kw)


async def test_rule_match_passes_threshold_and_block_rule_drops(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    # «реклама» нет в словаре намерений — без правила не прошёл бы порог
    assert await p.handle(lead("1", "Рекламному агентству требуется сайт-каталог")) is True
    await db.add_rule("+сайт +(нужен|требуется) -курс")
    assert await p.handle(lead("2", "Требуется сайт для автосервиса, звоните")) is True
    assert n.sent[-1][2].rule == "+сайт +(нужен|требуется) -курс"
    await db.add_rule("-вебинар -курс")
    assert await p.handle(lead("3", "Нужен сайт для курса по йоге, ищу разработчика")) is False


async def test_spammer_across_chats_is_ignored(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5, spam_chats=3)
    texts = ["Ищу кто сделает сайт, бюджет есть", "Нужен лендинг для курса, посоветуйте", "Кто делает ботов? нужен срочно"]
    for i, text in enumerate(texts):
        await p.handle(lead(f"{i}", text, contact="@spammer", extra={"chat_key": f"chat{i}"}))
    assert len(n.sent) == 2  # третий чат за час — рассылка исполнителя, автор уходит в игнор
    assert "@spammer" in (await db.get_ignored())["author"]
    assert await p.handle(lead("9", "Нужен сайт для кофейни", contact="@spammer", extra={"chat_key": "chat9"})) is False


async def test_near_duplicate_paraphrase_dropped(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    assert await p.handle(lead("1", "Нужен сайт для салона красоты, бюджет 30 тысяч, срок две недели")) is True
    assert await p.handle(lead("2", "Салону красоты нужен сайт! бюджет 30 тысяч, срок — две недели", source="vk")) is False
    assert len(n.sent) == 1


async def test_geo_filter_from_settings(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    await db.set_setting("geo", "Тула, Москва")
    assert await p.handle(lead("1", "Нужен сайт для кофейни в Казани")) is False
    assert await p.handle(lead("2", "Нужен сайт для кофейни в Туле")) is True
    assert await p.handle(lead("3", "Нужен лендинг для школы танцев")) is True


async def test_template_used_as_draft_without_llm(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    await db.set_template("сайт", "Здравствуйте! Делаем сайты под ключ.")
    assert await p.handle(lead("1", "Ищу кто сделает сайт для студии")) is True
    assert n.sent[0][2].draft == "Здравствуйте! Делаем сайты под ключ."


async def test_team_assign_and_events(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    await p.handle(lead("1", "Ищу кто сделает сайт для студии"))
    assert await db.assign(1, 10, "Аня") is True
    assert await db.assign(1, 11, "Боря") is False
    assert await db.assign(1, 10, "Аня") is True
    await db.set_status(1, "contacted", 10, "Аня")
    await db.schedule_followup(1, 0)
    assert [r["id"] for r in await db.followups_due()] == [1]
    await db.set_status(1, "deal", 10, "Аня")
    stats = {(r["user_name"], r["status"]): r["n"] for r in await db.team_stats(7)}
    assert stats[("Аня", "taken")] == 1 and stats[("Аня", "deal")] == 1
    assert (await db.funnel(7, "source"))[0]["deal"] == 1
    assert len(await db.response_times(7)) == 1


async def test_sla_overdue(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    await p.handle(lead("1", "Ищу кто сделает сайт и бота для студии, срочно, бюджет"))
    assert [r["id"] for r in await db.sla_overdue(0, 7)] == [1]
    await db.mark_sla_pinged(1)
    assert await db.sla_overdue(0, 7) == []


def test_route_topics():
    res = ScoreResult(5)
    assert route(Lead(source="yandex_maps", external_id="1", text="x"), Enriched(6, res), 8) == "business"
    assert route(Lead(source="site_audit", external_id="1", text="x"), Enriched(5, res), 8) == "audit"
    assert route(Lead(source="telegram", external_id="1", text="x"), Enriched(9, res, service="бот"), 8) == "hot"
    assert route(Lead(source="telegram", external_id="1", text="x"), Enriched(5, res, urgency="высокая"), 8) == "hot"
    assert route(Lead(source="kwork", external_id="1", text="x"), Enriched(5, res, service="сайт"), 8) == "boards"
    assert route(Lead(source="telegram", external_id="1", text="x"), Enriched(5, res, service="сайт"), 8) == "сайт"
    assert route(Lead(source="vk", external_id="1", text="x"), Enriched(5, res, service=""), 8) == "другое"
