"""Свежесть заказов с бирж: возраст, отклики, бонусы, лимит пачки на проход, досылка без протухшего."""
from datetime import datetime, timedelta, timezone

import pytest

from leadhunter.db import Database
from leadhunter.freshness import Freshness, check
from leadhunter.models import Lead
from leadhunter.pipeline import Pipeline
from leadhunter.sources.base import Source
from leadhunter.sources.freelance_ru import parse_freelance_ru_html
from leadhunter.sources.kwork import parse_kwork_html
from leadhunter.sources.weblancer import parse_weblancer_html

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def board(minutes_ago: int, competitors=None, source="kwork"):
    return Lead(source=source, external_id=f"{source}:{minutes_ago}:{competitors}", title="Нужен сайт", text="Лендинг для студии йоги",
                found_at=NOW - timedelta(minutes=minutes_ago), intent_implied=True, competitors=competitors)


def test_check_rules():
    f = Freshness()
    assert check(board(5, 0), f, NOW) == (True, 2, "5 мин назад · откликов нет")
    assert check(board(90, 7), f, NOW) == (True, 0, "90 мин назад · 7 откл.")
    assert check(board(200, 2), f, NOW)[0] is False  # старше 3 ч
    assert check(board(10, 40), f, NOW)[0] is False  # толпа откликов
    assert check(board(10, None), f, NOW) == (True, 1, "10 мин назад")  # источник не знает откликов
    ok, bonus, mark = check(Lead(source="telegram", external_id="t", text="x", found_at=NOW - timedelta(hours=20)), f, NOW)
    assert ok and bonus == 0 and mark == ""  # чаты живут по общему max_age
    custom = Freshness.from_settings({"board_max_age_hours": "0.5", "max_competitors": "3"})
    assert check(board(40, 0), custom, NOW)[0] is False and check(board(10, 4), custom, NOW)[0] is False


def test_parsers_extract_competitors():
    html = ('<script>window.stateData = {"wantsListData": {"pagination": {"data": [{"id": 1, "name": "Сайт", "description": "Нужен сайт",'
            ' "kwork_count": 34, "date_create": "2026-11-09 14:00:00", "isWantActive": true, "user": {}}]}}};</script>')
    assert parse_kwork_html(html)[0].competitors == 34
    card = ('<article class="task-card"><a class="task-card__title-link" href="/task/view/77">Сайт</a><p class="task-card__desc">Нужен сайт</p>'
            '<div class="task-card__foot"><span class="task-card__foot-item" title="09.11.2026 18:13"><i class="fa fa-clock-o"></i>2 часа</span>'
            '<span class="task-card__foot-item"><i class="fa fa-comments" aria-hidden="true"></i>\n<strong>18</strong></span></div></article>')
    assert parse_freelance_ru_html(card)[0].competitors == 18
    rsc = 'self.__next_f.push([1,"{\\"jobs\\":[{\\"id\\":5,\\"name\\":\\"Сайт\\",\\"description\\":\\"Нужен сайт\\",\\"status\\":\\"opened\\",\\"total_bids_count\\":13,\\"url\\":\\"/jobs/5\\"}]}"])'
    assert parse_weblancer_html(rsc)[0].competitors == 13


class Collect:
    def __init__(self):
        self.sent = []

    async def notify(self, lead_id, lead, info):
        self.sent.append((lead_id, lead.external_id, info.score, info.freshness))


async def test_pipeline_filters_stale_and_boosts_fresh(tmp_path):
    db = Database(str(tmp_path / "f.db"))
    await db.connect()
    n = Collect()
    p = Pipeline(db, n, default_threshold=5)
    now = datetime.now(timezone.utc)
    assert not await p.handle(Lead(source="kwork", external_id="old", title="Нужен сайт", text="Лендинг студии", intent_implied=True,
                                   found_at=now - timedelta(hours=5), competitors=2))
    assert not await p.handle(Lead(source="kwork", external_id="crowd", title="Нужен сайт", text="Лендинг студии йоги", intent_implied=True,
                                   found_at=now - timedelta(minutes=10), competitors=50))
    assert await p.handle(Lead(source="kwork", external_id="fresh", title="Нужен сайт", text="Лендинг для барбершопа", intent_implied=True,
                              found_at=now - timedelta(minutes=5), competitors=1))
    assert len(n.sent) == 1 and n.sent[0][2] >= 7 and "откл." in n.sent[0][3]
    row = await db.get_lead(n.sent[0][0])
    assert row["competitors"] == 1 and "мин назад" in row["freshness"]
    # /fresh меняет пороги на лету
    await db.set_setting("board_max_age_hours", "0.05")
    assert not await p.handle(Lead(source="kwork", external_id="fresh2", title="Нужен бот", text="Бот для записи клиентов", intent_implied=True,
                                   found_at=now - timedelta(minutes=10), competitors=0))
    # досылка не поднимает протухшие биржевые
    await db.set_setting("board_max_age_hours", "3")
    await p.handle(Lead(source="telegram", external_id="tg", text="Ищу кто сделает сайт для салона, бюджет 40000", found_at=now - timedelta(hours=10)), quiet=True)
    await db.conn.execute("UPDATE leads SET found_at = ? WHERE external_id = 'fresh'", ((now - timedelta(hours=6)).isoformat(),))
    await db.conn.commit()
    ids = [r["external_id"] for r in await db.undelivered_leads()]
    assert "tg" in ids and "fresh" not in ids
    await db.close()


async def test_source_burst_limit(tmp_path):
    db = Database(str(tmp_path / "b.db"))
    await db.connect()
    n = Collect()
    p = Pipeline(db, n, default_threshold=5)
    now = datetime.now(timezone.utc)

    class Board(Source):
        name = "kwork"
        interval = 1

        async def poll(self):
            return [Lead(source="kwork", external_id=str(i), title=f"Нужен сайт {i}", text=["Лендинг для барбершопа", "Интернет-магазин одежды на Tilda", "Сайт стоматологии с записью", "Квиз для ремонта квартир", "Корпоративный сайт завода", "Лендинг курса английского", "Сайт для фитнес-клуба с расписанием", "Каталог мебели с фильтрами", "Сайт юридической фирмы", "Портфолио фотографа", "Лендинг доставки еды", "Сайт автосервиса с онлайн-записью"][i], intent_implied=True,
                         found_at=now - timedelta(minutes=i), competitors=0) for i in range(12)]

    src = Board()
    src.burst_limit = 3
    emitted = []

    async def emit(lead, quiet=False):
        emitted.append((lead.external_id, quiet))
        return await p.handle(lead, quiet=quiet)

    import asyncio
    task = asyncio.create_task(src.run(emit))
    for _ in range(50):
        await asyncio.sleep(0.05)
        if len(emitted) >= 12:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(n.sent) == 3 and [e[1] for e in emitted[:3]] == [False, False, False] and emitted[3][1] is True
    assert [s[1] for s in n.sent] == ["0", "1", "2"]  # самые свежие
    cur = await db.conn.execute("SELECT COUNT(*) FROM leads")
    assert (await cur.fetchone())[0] >= 10  # остальные сохранены молча (пара ушла в семантический дедуп)
    await db.close()
