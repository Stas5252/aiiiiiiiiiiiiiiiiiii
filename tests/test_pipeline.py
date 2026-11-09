import pytest

from leadhunter.db import Database
from leadhunter.llm import Verdict
from leadhunter.models import Lead
from leadhunter.pipeline import Pipeline


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, lead_id, lead, score, res, summary):
        self.sent.append((lead_id, lead.source, score, summary))


class FakeLLM:
    def __init__(self, verdict):
        self.verdict = verdict

    async def classify(self, text):
        return self.verdict


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    yield db
    await db.close()


async def test_pipeline_filters_scores_and_dedups(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    good = Lead(source="telegram", external_id="1:1", text="Ищу кто сделает сайт для салона, бюджет есть")
    assert await p.handle(good) is True
    # тот же пост в другом чате — дубль по тексту
    assert await p.handle(Lead(source="telegram", external_id="2:9", text=good.text)) is False
    # тот же id — дубль по источнику
    assert await p.handle(good) is False
    assert await p.handle(Lead(source="telegram", external_id="1:2", text="Продам гараж")) is False
    assert await p.handle(Lead(source="telegram", external_id="1:3", text="Сделаю сайт недорого")) is False
    assert len(n.sent) == 1 and n.sent[0][2] >= 5


async def test_preset_score_bypasses_keywords(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    biz = Lead(source="yandex_maps", external_id="42", title="Кофейня", text="Кофейня, Москва. Сайта нет.", preset_score=6)
    assert await p.handle(biz) is True
    assert n.sent[0][2] == 6


async def test_llm_verdict_overrides_and_pause_suppresses(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5, llm=FakeLLM(Verdict(True, 9, "сайт", "лендинг для кофейни")))
    lead = Lead(source="vk", external_id="1", text="Нужен сайт для кофейни, посоветуйте")
    assert await p.handle(lead) is True
    assert n.sent[0][2] == 9 and n.sent[0][3] == "сайт: лендинг для кофейни"

    p.llm = FakeLLM(Verdict(False, 1, "другое", "реклама"))
    assert await p.handle(Lead(source="vk", external_id="2", text="Нужен сайт, посоветуйте студию")) is False

    await db.set_setting("paused", "1")
    p.llm = None
    assert await p.handle(Lead(source="vk", external_id="3", text="Ищу разработчика бота для магазина")) is True
    assert len(n.sent) == 1  # сохранили, но не уведомили
    assert len(await db.recent_leads()) == 2


async def test_threshold_setting_and_keywords(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    await db.set_setting("threshold", "9")
    assert await p.handle(Lead(source="vk", external_id="1", text="Ищу кто сделает сайт")) is False
    await db.set_setting("threshold", "5")
    await db.add_keyword("topic", "Квиз")
    assert await p.handle(Lead(source="vk", external_id="2", text="Нужен квиз для рекламы")) is True
    assert await db.remove_keyword("квиз") == 1
    assert "квиз" not in (await db.get_keywords()).topic
