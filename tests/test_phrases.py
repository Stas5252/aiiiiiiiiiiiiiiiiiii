import pytest

from leadhunter.db import Database
from leadhunter.http import in_work_hours
from leadhunter.phrases import PhraseProvider


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    yield db
    await db.close()


async def test_phrases_default_set_and_dedup(db):
    p = PhraseProvider(db, ["нужен сайт"])
    assert await p.get() == ["нужен сайт"]
    await p.set([" Нужен бот ", "нужен бот", "", "ищу разработчика"])
    assert await p.get() == ["нужен бот", "ищу разработчика"]
    await p.set([])
    assert await p.get() == ["нужен сайт"]


def test_work_hours():
    assert in_work_hours("0-24", "Europe/Moscow")
    assert in_work_hours("bad", "Europe/Moscow")
    # окно «ночь-утро» переходит через полночь
    assert in_work_hours("0-24", "UTC") is True
