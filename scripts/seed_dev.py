"""Наполняет data/dev.db настоящими лидами с публичных источников — для проверки Mini App."""
import asyncio
import logging

from leadhunter.db import Database
from leadhunter.phrases import PhraseProvider
from leadhunter.pipeline import Pipeline
from leadhunter.sources.kwork import KworkSource
from leadhunter.sources.threads_web import ThreadsWebSource
from leadhunter.sources.weblancer import WeblancerSource
from leadhunter.sources.yandex_maps_web import YandexMapsWebSource


class Printer:
    async def notify(self, lead_id, lead, info):
        print(f"  + #{lead_id} {lead.source} {info.score}/10 {(lead.title or lead.text)[:60]!r}")


async def main():
    logging.basicConfig(level=logging.WARNING)
    db = Database("data/dev.db")
    await db.connect()
    p = Pipeline(db, Printer(), default_threshold=5)
    ph = PhraseProvider(db, ["нужен сайт", "ищу разработчика бота"])
    for src in [KworkSource(["11"], 2, 600, None), WeblancerSource(["https://www.weblancer.net/jobs/"], 900, None),
                ThreadsWebSource(ph, 900, None), YandexMapsWebSource(["Тула"], ["стоматология", "автосервис"], 3600, None, audit=True, audit_per_poll=10)]:
        try:
            leads = await src.poll()
            n = sum([await p.handle(l) for l in leads])
            print(f"{src.name}: {len(leads)} записей, {n} лидов")
        except Exception as e:
            print(src.name, "ERR", e)
    # немного «жизни» для статистики
    await db.add_member(1, "Стас")
    rows = await db.recent_leads(6)
    if len(rows) >= 4:
        await db.assign(rows[0]["id"], 1, "Стас"); await db.set_status(rows[0]["id"], "contacted", 1, "Стас")
        await db.assign(rows[1]["id"], 1, "Стас"); await db.set_status(rows[1]["id"], "deal", 1, "Стас")
        await db.set_status(rows[2]["id"], "spam", 1, "Стас")
    await db.add_rule("+сайт +(нужен|ищу|требуется) -предлагаю")
    await db.set_template("сайт", "Здравствуйте! Увидел ваш запрос на сайт. Делаем под ключ за 7–14 дней. Подскажите, есть ли уже структура или примеры, которые нравятся?")
    await db.close()


asyncio.run(main())
