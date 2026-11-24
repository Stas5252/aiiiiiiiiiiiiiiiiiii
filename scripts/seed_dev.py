"""Наполняет базу разработчика лидами с публичных площадок — для проверки конвейера, карточек и Mini App.

    python scripts/seed_dev.py                      # живые площадки, база data/dev.db
    python scripts/seed_dev.py --copy-from data/leadhunter.db --db data/seed.db   # на копии рабочей базы
    python scripts/seed_dev.py --offline            # без сети: сохранённые страницы из tests/fixtures

Рабочую базу напрямую не трогаем: только её копию (--copy-from). Уведомления никуда не уходят — печатаются.
Живой режим делает по одному запросу на площадку с паузами. Код выхода 1 — если хотя бы одна из пяти
площадок (Kwork, Weblancer, FL.ru, Freelance.ru, Яндекс.Карты) не отдала записей или её лидов нет в базе.
Повторный прогон по той же базе новых лидов почти не даёт — заказы уже в ней, это работа дедупа."""
import argparse
import asyncio
import logging
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leadhunter.config import Settings  # noqa: E402
from leadhunter.db import Database  # noqa: E402
from leadhunter.models import Lead  # noqa: E402
from leadhunter.pipeline import Pipeline  # noqa: E402
from leadhunter.sources.freelance_ru import FreelanceRuSource, parse_freelance_ru_html  # noqa: E402
from leadhunter.sources.kwork import KworkSource, parse_kwork_html  # noqa: E402
from leadhunter.sources.rss_boards import RSSBoardsSource, parse_feed  # noqa: E402
from leadhunter.sources.weblancer import WeblancerSource, parse_weblancer_html  # noqa: E402
from leadhunter.sources.yandex_maps_web import YandexMapsWebSource, business_lead, parse_yandex_items  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
FL_RSS = "https://www.fl.ru/rss/all.xml"
# площадка → как её лиды называются в базе (leads.source)
PLACES = {"Kwork": "kwork", "Weblancer": "weblancer", "FL.ru": "rss%", "Freelance.ru": "freelance.ru",
          "Яндекс.Карты": "yandex_maps"}


class Printer:
    async def notify(self, lead_id, lead, info):
        print(f"  + #{lead_id} {lead.source} {info.score}/10 {(lead.title or lead.text)[:60]!r}")
        return True


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _refresh(leads: list[Lead]) -> list[Lead]:
    """Страницы в фикстурах сняты давно, а конвейер отсекает старые заказы: сдвигаем даты к «сейчас»,
    сохраняя порядок и промежутки между заказами (самый свежий — 5 минут назад, не дальше 2 часов)."""
    if not leads:
        return leads
    now = datetime.now(timezone.utc)
    aware = [l.found_at if l.found_at.tzinfo else l.found_at.replace(tzinfo=timezone.utc) for l in leads]
    newest = max(aware)
    for lead, at in zip(leads, aware):
        lead.found_at = now - timedelta(minutes=5) - min(newest - at, timedelta(hours=2))
    return leads


async def offline_polls() -> dict[str, list[Lead]]:
    maps = [business_lead(item, "ремонт обуви", "Тула")
            for item in parse_yandex_items(_fixture("yandex_maps_search.html")) if not item.get("urls")]
    return {
        "Kwork": _refresh(parse_kwork_html(_fixture("kwork_projects.html"))),
        "Weblancer": _refresh(parse_weblancer_html(_fixture("weblancer_jobs.html"))),
        "FL.ru": _refresh(parse_feed((FIXTURES / "fl_ru.xml").read_bytes(), FL_RSS)),
        "Freelance.ru": _refresh(parse_freelance_ru_html(_fixture("freelance_ru_tasks.html"))),
        "Яндекс.Карты": maps,
    }


async def live_polls() -> dict[str, list[Lead]]:
    settings = Settings(bot_token="1:SEED", _env_file=None, rss_feeds=[FL_RSS])
    sources = {
        "Kwork": KworkSource(["11"], 1, 600, None),
        "Weblancer": WeblancerSource(["https://www.weblancer.net/jobs/"], 900, None),
        "FL.ru": RSSBoardsSource(settings),
        "Freelance.ru": FreelanceRuSource(["сайт"], 900, None),
        "Яндекс.Карты": YandexMapsWebSource(["Тула"], ["ремонт обуви"], 3600, None, audit=False),
    }
    out: dict[str, list[Lead]] = {}
    for place, src in sources.items():
        try:
            out[place] = await src.poll()
        except Exception as e:
            print(f"{place}: ОШИБКА — {type(e).__name__}: {e}")
            out[place] = []
    return out


async def seed(db_path: str, *, offline: bool = False, copy_from: str | None = None) -> dict[str, int]:
    """Возвращает {площадка: {"records": отдала записей, "new": новых лидов, "total": её лидов в базе}}."""
    if copy_from:
        if Path(copy_from).resolve() == Path(db_path).resolve():
            raise SystemExit("--copy-from и --db указывают на один файл: работать можно только с копией")
        shutil.copyfile(copy_from, db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)
    await db.connect()
    try:
        # заказы бирж в выдаче лежат сутками: для наполнения берём всё, что площадка показывает сейчас
        await db.set_setting("board_max_age_hours", "48")
        await db.set_setting("max_competitors", "1000")
        pipeline = Pipeline(db, Printer(), default_threshold=5)
        polls = await (offline_polls() if offline else live_polls())
        report = {}
        for place, source in PLACES.items():
            leads = polls.get(place, [])
            new = sum([await pipeline.handle(lead) for lead in leads])
            cur = await db.conn.execute("SELECT COUNT(*) FROM leads WHERE source LIKE ?", (source,))
            report[place] = {"records": len(leads), "new": new, "total": (await cur.fetchone())[0]}
            print(f"{place}: записей {len(leads)}, новых лидов {new}, всего в базе {report[place]['total']}")
        await _add_life(db)
        return report
    finally:
        await db.close()


async def _add_life(db: Database) -> None:
    """Немного «жизни» для статистики и воронки."""
    await db.add_member(1, "Стас")
    rows = await db.recent_leads(6)
    if len(rows) >= 4:
        await db.assign(rows[0]["id"], 1, "Стас")
        await db.set_status(rows[0]["id"], "contacted", 1, "Стас")
        await db.assign(rows[1]["id"], 1, "Стас")
        await db.set_status(rows[1]["id"], "deal", 1, "Стас")
        await db.set_status(rows[2]["id"], "spam", 1, "Стас")
    await db.add_rule("+сайт +(нужен|ищу|требуется) -предлагаю")
    await db.set_template("сайт", "Здравствуйте! Увидел ваш запрос на сайт. Делаем под ключ за 7–14 дней. "
                                  "Подскажите, есть ли уже структура или примеры, которые нравятся?")


def main() -> int:
    ap = argparse.ArgumentParser(description="Наполнить базу разработчика лидами с публичных площадок")
    ap.add_argument("--db", default="data/dev.db", help="база, которую наполняем (по умолчанию data/dev.db)")
    ap.add_argument("--copy-from", help="сначала скопировать эту базу в --db и работать с копией")
    ap.add_argument("--offline", action="store_true", help="без сети: сохранённые страницы из tests/fixtures")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    report = asyncio.run(seed(args.db, offline=args.offline, copy_from=args.copy_from))
    empty = [place for place, r in report.items() if not r["records"] or not r["total"]]
    if empty:
        print("Площадка не отдала записей или её лидов нет в базе: " + ", ".join(empty))
        return 1
    print(f"Готово: в {args.db} есть лиды со всех пяти площадок")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
