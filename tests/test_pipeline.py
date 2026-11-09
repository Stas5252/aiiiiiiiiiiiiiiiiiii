from datetime import datetime, timedelta, timezone

import pytest

from leadhunter.db import Database
from leadhunter.llm import Verdict
from leadhunter.models import Lead
from leadhunter.pipeline import Pipeline


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, lead_id, lead, info):
        self.sent.append((lead_id, lead, info))


class FakeLLM:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    async def classify(self, text, profile, good=(), bad=()):
        self.calls.append((text, profile, list(good), list(bad)))
        return self.verdict


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    yield db
    await db.close()


def lead(ext, text, **kw):
    return Lead(source=kw.pop("source", "telegram"), external_id=ext, text=text, **kw)


async def test_pipeline_filters_scores_and_dedups(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    good = lead("1:1", "Ищу кто сделает сайт для салона, бюджет 40 000 руб, пишите @anna_salon")
    assert await p.handle(good) is True
    # тот же пост в другом чате с другой ссылкой — дубль по тексту
    assert await p.handle(lead("2:9", good.text + " https://t.me/x/1 🔥")) is False
    assert await p.handle(good) is False
    assert await p.handle(lead("1:2", "Продам гараж недорого")) is False
    assert await p.handle(lead("1:3", "Сделаю сайт недорого, портфолио")) is False
    assert len(n.sent) == 1
    _, saved, info = n.sent[0]
    assert info.score >= 5 and info.service == "сайт"
    assert info.budget == "40 000 руб" and saved.contact == "@anna_salon"


async def test_preset_score_bypasses_keywords_and_llm(db):
    n = FakeNotifier()
    llm = FakeLLM(Verdict(False, 0))
    p = Pipeline(db, n, default_threshold=5, llm=llm)
    biz = lead("42", "Кофейня, Москва. Сайта нет.", source="yandex_maps", title="Кофейня", preset_score=6)
    assert await p.handle(biz) is True
    assert n.sent[0][2].score == 6 and not llm.calls


async def test_llm_gets_profile_and_feedback_examples(db):
    n = FakeNotifier()
    llm = FakeLLM(Verdict(True, 9, "сайт", "лендинг для кофейни", "высокая", "", "Здравствуйте! ..."))
    p = Pipeline(db, n, default_threshold=5, llm=llm, profile="Делаем сайты")
    await db.set_setting("profile", "Студия X: сайты и боты")
    assert await p.handle(lead("1", "Нужен сайт для кофейни, посоветуйте", source="vk")) is True
    assert llm.calls[0][1] == "Студия X: сайты и боты"
    info = n.sent[0][2]
    assert info.score == 9 and info.draft == "Здравствуйте! ..." and info.urgency == "высокая"

    # отметки владельца попадают в следующий промпт
    await db.set_status(1, "hot")
    await p.handle(lead("2", "Нужен бот для записи клиентов", source="vk"))
    assert llm.calls[1][2] == ["Нужен сайт для кофейни, посоветуйте"]


async def test_llm_sees_borderline_text_and_can_reject(db):
    n = FakeNotifier()
    llm = FakeLLM(Verdict(False, 1, "другое", "реклама"))
    p = Pipeline(db, n, default_threshold=5, llm=llm, llm_min_score=2)
    # только слово по теме, без намерения — без ИИ отсеялось бы, с ИИ отправляется на проверку
    assert await p.handle(lead("1", "Обсуждаем сайты в этом чате каждый день", source="vk")) is False
    assert len(llm.calls) == 1
    assert await p.handle(lead("2", "Погода сегодня отличная, правда?", source="vk")) is False
    assert len(llm.calls) == 1


async def test_llm_failure_falls_back_to_keywords(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5, llm=FakeLLM(None))
    assert await p.handle(lead("1", "Ищу разработчика бота для магазина", source="vk")) is True
    assert n.sent[0][2].summary is None


async def test_pause_ignore_lists_and_freshness(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5, max_age_hours=24)
    await db.set_setting("paused", "1")
    assert await p.handle(lead("1", "Ищу разработчика бота для магазина")) is True
    assert not n.sent and len(await db.recent_leads()) == 1
    await db.set_setting("paused", "0")

    await db.add_ignored("chat", "777", "Шумный чат")
    assert await p.handle(lead("2", "Нужен сайт для кофейни срочно", extra={"chat_key": "777"})) is False
    await db.add_ignored("author", "@spammer")
    assert await p.handle(lead("3", "Нужен сайт для кофейни быстро", contact="@spammer")) is False

    old = datetime.now(timezone.utc) - timedelta(hours=30)
    assert await p.handle(lead("4", "Нужен лендинг для курса йоги", found_at=old)) is False
    assert await p.handle(lead("5", "Нужен лендинг для студии йоги")) is True


async def test_threshold_setting_and_keywords(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    await db.set_setting("threshold", "9")
    assert await p.handle(lead("1", "Ищу кто сделает сайт", source="vk")) is False
    await db.set_setting("threshold", "5")
    await db.add_keyword("topic", "Квиз-опрос", "сайт")
    assert await p.handle(lead("2", "Нужен квиз-опрос для рекламы", source="vk")) is True
    assert n.sent[-1][2].service == "сайт"
    assert await db.remove_keyword("квиз-опрос") == 1
    assert "квиз-опрос" not in (await db.get_keywords()).topic


async def test_export_csv(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    await p.handle(lead("1", "Ищу кто сделает сайт для салона"))
    data = (await db.export_csv()).decode("utf-8-sig")
    assert data.splitlines()[0].startswith("id,created_at,source,score")
    assert "Ищу кто сделает сайт" in data
