"""Модуль B: конвейер, база, доставка и фоновые циклы (находки 6, 9–12, 16, 18–22 и K1)."""
import csv
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from leadhunter import loops
from leadhunter.bot.notify import TelegramNotifier
from leadhunter.db import Database
from leadhunter.models import Lead
from leadhunter.pipeline import Pipeline

UTC = timezone.utc


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, lead_id, lead, info):
        self.sent.append((lead_id, lead, info))


class FakeBot:
    """Заглушка Bot API: считает сообщения, по флагу down имитирует сбой Telegram."""

    def __init__(self):
        self.down = False
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.down:
            raise RuntimeError("Telegram недоступен")
        self.messages.append((chat_id, text))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=len(self.messages), message_thread_id=None)


class QuotaLLM:
    max_per_hour = 2
    quota_exhausted = True

    def quota_resets_in(self):
        return 17

    async def classify(self, text, profile, good=(), bad=()):
        return None


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def tg(db, bot):
    return TelegramNotifier(bot, db, [777])


def lead(ext, text, **kw):
    return Lead(source=kw.pop("source", "telegram"), external_id=ext, text=text, **kw)


async def age_lead(db, lead_id, **delta):
    at = (datetime.now(UTC) - timedelta(**delta)).isoformat()
    await db.conn.execute("UPDATE leads SET created_at = ? WHERE id = ?", (at, lead_id))
    await db.conn.commit()


# --- находка 6: спам-детект ---

async def test_chatter_in_many_chats_does_not_ban_author(db):
    """Обычные реплики в трёх чатах — не рассылка: настоящий заказ того же автора проходит."""
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5, spam_chats=3)
    for i, text in enumerate(["Всем привет, как дела у вас?", "Кто идёт на митап в пятницу?", "Спасибо, очень помогли вчера"]):
        assert await p.handle(lead(f"{i}:1", text, contact="@client", extra={"chat_key": str(i)})) is False
    assert (await db.get_ignored())["author"] == {}
    assert await p.handle(lead("9:1", "Ищу кто сделает сайт для салона, бюджет 40 000 руб", contact="@client",
                               extra={"chat_key": "9"})) is True
    assert len(n.sent) == 1


async def test_mass_requests_ban_is_temporary_and_visible(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5, spam_chats=3)
    texts = ["Ищу кто сделает сайт для салона красоты", "Нужен бот для записи клиентов в барбершоп, срочно",
             "Нужен лендинг для школы английского, подскажите"]
    results = [await p.handle(lead(f"{i}:1", t, contact="@mailer", extra={"chat_key": str(i)}))
               for i, t in enumerate(texts)]
    assert results == [True, True, False]
    ignored = (await db.get_ignored())["author"]
    assert "@mailer" in ignored and "24 ч" in ignored["@mailer"]
    alerts = await db.alerts()
    assert len(alerts) == 1 and "@mailer" in alerts[0]["text"] and "/unblock" in alerts[0]["text"]
    assert await p.handle(lead("5:1", "Нужен сайт для кофейни, срочно", contact="@mailer", extra={"chat_key": "5"})) is False

    # через сутки игнор снимается сам
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await db.conn.execute("UPDATE ignored SET until = ? WHERE key = '@mailer'", (past,))
    await db.conn.execute("DELETE FROM author_events")
    await db.conn.commit()
    assert (await db.get_ignored())["author"] == {}
    assert await p.handle(lead("6:1", "Нужен интернет-магазин для пекарни", contact="@mailer", extra={"chat_key": "6"})) is True


async def test_manual_block_is_permanent(db):
    await db.add_ignored("author", "@spammer")
    await db.cleanup()
    assert "@spammer" in (await db.get_ignored())["author"]


# --- находка 12: порог и лиды с карт ---

async def test_preset_score_is_not_cut_by_threshold(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    await db.set_setting("threshold", "8")
    assert await p.handle(lead("42", "Кофейня, Тула. Сайта в карточке нет.", source="yandex_maps", title="Кофейня",
                               preset_score=6)) is True
    assert await p.handle(lead("audit:43", "Автосервис, Тула. Сайт a.ru: не открывается с телефона.",
                               source="site_audit", title="Автосервис", preset_score=5)) is True
    assert [i.score for _, _, i in n.sent] == [6, 5]
    # обычный текст порогу по-прежнему подчиняется, а карты выключаются своей группой
    assert await p.handle(lead("1", "Подскажите, кто делал сайт на тильде?", source="vk")) is False  # 7 < 8
    await db.set_setting("leads_maps", "0")
    assert await p.handle(lead("44", "Пекарня, Тула. Сайта в карточке нет.", source="yandex_maps", preset_score=6)) is False


# --- K1: заявки с лендинга ---

def application(n, name, message=""):
    text = ("Заявка с лендинга «Чек-лист запуска сайта»" + (f": {message}" if message else "")
            + f"\nИмя: {name}. Хочет получить материал и консультацию.")
    return Lead(source="landing", external_id=f"checklist:{n}", text=text, title="Чек-лист запуска сайта",
                author=name, contact=f"+7999000000{n}", preset_score=9)


async def test_landing_applications_are_never_lost(db):
    n = FakeNotifier()
    p = Pipeline(db, n, default_threshold=5)
    await db.set_setting("threshold", "10")
    await db.set_setting("geo", "Казань")
    await db.add_rule("-заявка")
    await db.add_ignored("author", "аноним")
    # девять разных людей, шаблонный текст, одинаковое имя — раньше проходила одна заявка
    for i in range(9):
        assert await p.handle(application(i, "Аноним")) is True
    assert len(n.sent) == 9 and len(await db.recent_leads(20)) == 9
    assert {l.contact for _, l, _ in n.sent} == {f"+7999000000{i}" for i in range(9)}
    assert all(i.score == 9 for _, _, i in n.sent)
    # повторная отправка той же заявки (тот же external_id) второй лид не создаёт
    assert await p.handle(application(3, "Аноним")) is False


async def test_landing_application_survives_pause(db, tg, bot):
    p = Pipeline(db, tg, default_threshold=5)
    await db.set_setting("paused", "1")
    assert await p.handle(application(1, "Анна", "нужен сайт для салона")) is True
    assert await loops.redeliver_tick(db, tg) == 0 and not bot.messages
    await db.set_setting("paused", "0")
    assert await loops.redeliver_tick(db, tg) == 1
    assert len(bot.messages) == 1 and "Анна" in bot.messages[0][1]


# --- находка 16: семантический дедуп после перезапуска ---

async def test_near_duplicates_survive_restart(db):
    n = FakeNotifier()
    first = "Нужен сайт для салона красоты в центре города, бюджет 30 000 руб, срок две недели"
    again = "Салону красоты в центре города нужен сайт, бюджет 30 000 руб, срок две недели, пишите"
    assert await Pipeline(db, n, default_threshold=5).handle(lead("1", first, source="vk")) is True
    restarted = Pipeline(db, n, default_threshold=5)  # новый процесс: индекс в памяти пуст
    assert await restarted.handle(lead("2", again, source="vk")) is False
    assert await restarted.handle(lead("3", "Ищу кто сделает бота для записи в автосервис", source="vk")) is True
    assert len(n.sent) == 2


# --- находка 18: лимит ИИ ---

async def test_llm_quota_alert_reaches_owner_once(db, tg, bot):
    p = Pipeline(db, FakeNotifier(), default_threshold=5, llm=QuotaLLM())
    assert await p.handle(lead("1", "Ищу разработчика бота для магазина", source="vk")) is True
    assert await p.handle(lead("2", "Нужен сайт для кофейни, срочно", source="vk")) is True
    alerts = await db.alerts()
    assert len(alerts) == 1 and "Лимит ИИ исчерпан" in alerts[0]["text"] and "17 мин" in alerts[0]["text"]
    assert await loops.alerts_tick(db, tg) == 1
    assert "только по словарю" in bot.messages[0][1]
    assert await db.alerts() == [] and await loops.alerts_tick(db, tg) == 0


async def test_alert_is_kept_until_delivered(db, tg, bot):
    await db.add_alert("x", "⚠️ проверка")
    bot.down = True
    assert await loops.alerts_tick(db, tg) == 0 and len(await db.alerts()) == 1
    bot.down = False
    assert await loops.alerts_tick(db, tg) == 1 and await db.alerts() == []


# --- находки 9–11: SLA, follow-up, досылка ---

async def test_send_reports_success(db, tg, bot):
    assert await tg.send("привет") is True
    bot.down = True
    assert await tg.send("привет") is False


async def test_sla_ping_is_marked_only_after_delivery(db, tg, bot):
    p = Pipeline(db, tg, default_threshold=5)
    assert await p.handle(lead("1", "Ищу кто сделает сайт и бота для студии, срочно, бюджет")) is True
    await age_lead(db, 1, minutes=15)
    bot.down = True
    assert await loops.sla_tick(db, tg, 10, 7) == 0
    assert (await db.get_lead(1))["sla_pinged"] == 0
    bot.down = False
    assert await loops.sla_tick(db, tg, 10, 7) == 1
    assert (await db.get_lead(1))["sla_pinged"] == 1 and "никто не взял 15 мин" in bot.messages[-1][1]
    assert await loops.sla_tick(db, tg, 10, 7) == 0


async def test_sla_skips_quiet_stale_and_paused_leads(db, tg, bot):
    p = Pipeline(db, tg, default_threshold=5)
    texts = ["Ищу кто сделает сайт и бота для студии, срочно, бюджет", "Нужен бот и сайт для клиники, срочно, бюджет есть",
             "Срочно нужен лендинг и чат-бот для школы, бюджет есть"]
    assert await p.handle(lead("1", texts[0]), quiet=True) is True   # «тихий»: карточки не было
    assert await p.handle(lead("2", texts[1])) is True               # доставлен, но бот простаивал сутки
    assert await p.handle(lead("3", texts[2])) is True               # доставлен 15 минут назад
    assert len(bot.messages) == 2
    await age_lead(db, 1, minutes=15)
    await age_lead(db, 2, hours=30)
    await age_lead(db, 3, minutes=15)
    assert [r["id"] for r in await db.sla_overdue(10, 7)] == [3]
    await db.set_setting("paused", "1")
    assert await loops.sla_tick(db, tg, 10, 7) == 0
    await db.set_setting("paused", "0")
    assert await loops.sla_tick(db, tg, 10, 7) == 1
    assert len(bot.messages) == 3


async def test_sla_backlog_is_limited(db, tg, bot):
    p = Pipeline(db, tg, default_threshold=5)
    services = ["салона", "клиники", "школы", "кофейни", "автосервиса", "магазина", "фитнес-клуба", "отеля"]
    for i, s in enumerate(services):
        assert await p.handle(lead(str(i), f"Ищу кто сделает сайт и бота для {s}, срочно, бюджет есть")) is True
        await age_lead(db, i + 1, minutes=12)
    before = len(bot.messages)
    assert await loops.sla_tick(db, tg, 10, 7) == 5
    assert len(bot.messages) - before == 5


async def test_followup_is_cleared_only_after_delivery(db, tg, bot):
    p = Pipeline(db, tg, default_threshold=5)
    await p.handle(lead("1", "Ищу кто сделает сайт для салона, бюджет 40 000 руб"))
    await db.set_status(1, "contacted")
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await db.conn.execute("UPDATE leads SET followup_at = ?, followup_count = 1 WHERE id = 1", (past,))
    await db.conn.commit()
    bot.down = True
    assert await loops.followup_tick(db, tg) == 0
    assert (await db.get_lead(1))["followup_at"] == past
    bot.down = False
    assert await loops.followup_tick(db, tg) == 1
    assert (await db.get_lead(1))["followup_at"] is None and "Follow-up #1" in bot.messages[-1][1]


async def test_undelivered_lead_is_redelivered_in_background(db, tg, bot):
    p = Pipeline(db, tg, default_threshold=5)
    bot.down = True
    assert await p.handle(lead("1", "Ищу кто сделает сайт для салона, бюджет 40 000 руб")) is True
    assert (await db.get_lead(1))["msg_id"] is None
    assert await loops.redeliver_tick(db, tg) == 0  # Telegram всё ещё лежит
    bot.down = False
    assert await loops.redeliver_tick(db, tg) == 1
    row = await db.get_lead(1)
    assert row["msg_id"] is not None and "Ищу кто сделает сайт" in bot.messages[0][1]
    assert await loops.redeliver_tick(db, tg) == 0 and len(bot.messages) == 1


async def test_redelivery_skips_quiet_stale_and_hopeless_leads(db, tg, bot):
    p = Pipeline(db, tg, default_threshold=5)
    bot.down = True
    assert await p.handle(lead("1", "Ищу кто сделает сайт для салона, бюджет 40 000 руб"), quiet=True) is True
    assert await p.handle(lead("2", "Нужен бот для записи клиентов в барбершоп, срочно")) is True
    assert await p.handle(lead("3", "Нужен лендинг для школы английского, подскажите", source="kwork",
                               found_at=datetime.now(UTC) - timedelta(hours=1))) is True
    await age_lead(db, 2, hours=72)                      # лид из чата трёхдневной давности
    await db.conn.execute("UPDATE leads SET found_at = ? WHERE id = 3",
                          ((datetime.now(UTC) - timedelta(hours=5)).isoformat(),))  # заказ биржи уже разобрали
    await db.conn.commit()
    bot.down = False
    assert await loops.redeliver_tick(db, tg) == 0 and not bot.messages

    # лид, который не уходит совсем: после пяти попыток — алерт, дальше не пытаемся
    assert await p.handle(lead("4", "Нужен интернет-магазин для пекарни, бюджет есть")) is True
    await db.conn.execute("UPDATE leads SET msg_id = NULL, pending = 1 WHERE id = 4")
    await db.conn.commit()
    bot.messages.clear()
    bot.down = True
    for _ in range(6):
        await loops.redeliver_tick(db, tg)
    assert (await db.get_lead(4))["attempts"] == 5
    assert [a["key"] for a in await db.alerts()] == ["undelivered:4"]


# --- находка 19: CSV ---

async def test_csv_export_neutralizes_formulas(db):
    p = Pipeline(db, FakeNotifier(), default_threshold=5)
    evil = '=HYPERLINK("http://evil.example","нужен сайт, ищу подрядчика")'
    assert await p.handle(lead("1", evil, source="vk", author="+cmd|calc", contact="@client_x")) is True
    rows = list(csv.DictReader(io.StringIO((await db.export_csv()).decode("utf-8-sig"))))
    assert rows[0]["text"] == "'" + evil
    assert rows[0]["author"] == "'+cmd|calc" and rows[0]["contact"] == "'@client_x"
    assert rows[0]["id"] == "1" and rows[0]["source"] == "vk"


# --- находка 20: found_at в UTC ---

async def test_found_at_is_stored_in_utc(db):
    p = Pipeline(db, FakeNotifier(), default_threshold=5)
    msk = timezone(timedelta(hours=3))
    five_hours_ago = (datetime.now(UTC) - timedelta(hours=5)).astimezone(msk)
    await db.set_setting("board_max_age_hours", "6")
    assert await p.handle(lead("1", "Нужен лендинг для школы английского", source="kwork", found_at=five_hours_ago,
                               intent_implied=True)) is True
    stored = (await db.get_lead(1))["found_at"]
    assert stored.endswith("+00:00") and datetime.fromisoformat(stored) == five_hours_ago
    # заказу 5 часов: в «моложе 3 ч» он попадать не должен
    assert await db.undelivered_leads(20, board_max_age_hours=3) == []
    assert len(await db.undelivered_leads(20, board_max_age_hours=6)) == 1


async def test_old_rows_are_migrated_to_utc(tmp_path):
    path = str(tmp_path / "old.db")
    db = Database(path)
    await db.connect()
    await db.conn.execute(
        "INSERT INTO leads (source, external_id, score, found_at, created_at) VALUES ('kwork', '1', 6, ?, ?)",
        ("2026-11-22T23:20:31+03:00", "2026-11-22T20:25:00+00:00"))
    await db.conn.commit()
    await db.close()
    db = Database(path)
    await db.connect()
    assert (await db.get_lead(1))["found_at"] == "2026-11-22T20:20:31+00:00"
    await db.close()


# --- находка 21: дайджест ---

def test_digest_survives_clock_change():
    berlin = ZoneInfo("Europe/Berlin")
    # осень: сутки длиной 25 часов — один дайджест в 9:00, а не в 8:00 и ещё раз в 9:00
    sent = datetime(2026, 10, 24, 9, 0, tzinfo=berlin)
    nxt = loops.next_digest_at(sent, 9, berlin)
    assert nxt.astimezone(berlin) == datetime(2026, 10, 25, 9, 0, tzinfo=berlin)
    assert nxt - sent.astimezone(UTC) == timedelta(hours=25)
    assert loops.next_digest_at(nxt, 9, berlin).astimezone(berlin) == datetime(2026, 10, 26, 9, 0, tzinfo=berlin)
    # весна: сутки длиной 23 часа — дайджест в 9:00, а не в 10:00
    sent = datetime(2026, 3, 28, 9, 0, tzinfo=berlin)
    nxt = loops.next_digest_at(sent, 9, berlin)
    assert nxt.astimezone(berlin).hour == 9 and nxt - sent.astimezone(UTC) == timedelta(hours=23)
    # Москва без перевода часов
    msk = ZoneInfo("Europe/Moscow")
    nxt = loops.next_digest_at(datetime(2026, 11, 23, 8, 59, tzinfo=msk), 9, msk)
    assert nxt.astimezone(msk) == datetime(2026, 11, 23, 9, 0, tzinfo=msk)


@pytest.mark.parametrize("hour, tz, word", [(9, "Moscow", "DIGEST_TZ"), (24, "Europe/Moscow", "DIGEST_HOUR"),
                                            (-1, "Europe/Moscow", "DIGEST_HOUR"), ("9", "Europe/Moscow", "DIGEST_HOUR")])
async def test_bad_digest_settings_do_not_crash_silently(db, tg, bot, hour, tz, word):
    assert word in loops.digest_settings_error(hour, tz)
    await loops.digest_loop(db, tg, hour, tz)  # раньше — исключение и молча умершая задача
    assert await loops.alerts_tick(db, tg) == 1
    assert "дайджест выключен" in bot.messages[0][1] and word in bot.messages[0][1]


def test_good_digest_settings():
    assert loops.digest_settings_error(9, "Europe/Moscow") is None
    assert loops.digest_settings_error(0, "Asia/Yekaterinburg") is None


# --- находка 22: уборка ---

async def test_cleanup_removes_old_service_rows(db):
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    await db.mark_seen("kwork", "old")
    await db.mark_seen("kwork", "fresh")
    await db.mark_seen("yandex_maps", "org")
    await db.mark_hash_seen("старый текст заказа")
    await db.mark_hash_seen("свежий текст заказа")
    await db.author_seen("@a", "1")
    await db.author_seen("@b", "1")
    await db.conn.execute("UPDATE seen SET seen_at = ? WHERE external_id IN ('old', 'org')", (old,))
    await db.conn.execute("UPDATE seen_hashes SET seen_at = ? WHERE rowid = 1", (old,))
    await db.conn.execute("UPDATE author_events SET at = ? WHERE author = '@a'", (old,))
    await db.conn.commit()
    # организация с карт встретилась снова — запись живёт дальше и после уборки лидом не вернётся
    assert await db.mark_seen("yandex_maps", "org") is False

    removed = await db.cleanup(days=30)
    assert (removed["seen"], removed["seen_hashes"], removed["author_events"]) == (1, 1, 1)
    assert await db.mark_seen("kwork", "fresh") is False
    assert await db.mark_seen("yandex_maps", "org") is False
    assert await db.mark_seen("kwork", "old") is True
    assert await db.mark_hash_seen("свежий текст заказа") is False
