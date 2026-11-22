# LeadHunter — проверка области «Поиск лидов: источники и конвейер»

Проверялось: `leadhunter/sources/*`, `http.py`, `pipeline.py`, `scoring.py`, `filters.py`, `freshness.py`, `dedup.py`, `geo.py`, `extract.py`, `audit.py`, `llm.py`, `db.py`, `models.py`, `webhook.py`, `loops.py`.

Условия: Python 3.12.16, `pip install -e ".[dev]"`, `pytest` → **211 passed за 14.7 с**. Машина проверки — сервер с часовым поясом UTC; судя по редиректу Яндекса на yandex.com и ответу hh.ru 403, её IP — дата-центр вне РФ (это важно для Threads и hh.ru — см. оговорки). Настоящий `BOT_TOKEN` не использовался и к Telegram Bot API запросов не было: везде `BOT_TOKEN=123456:TESTDUMMY`, бот заменён заглушкой, база — копия `data/leads.db`. Код проекта не менялся. Каждая площадка вызвана 1–3 раза; Threads — 12 одиночных запросов с паузами (10 разных фраз).

## 1. Живые источники без ключей (22–23.11.2026)

| Источник | README | Что вернулось на самом деле | Вердикт |
|---|---|---|---|
| Kwork «Запросы» (`c=11`, стр. 1) | ✅ | HTTP 200, **12 заказов**, возраст 1.1–47.9 ч, у всех есть число откликов, автор, ссылка. Пример: `3274956 · «Допиливание мобильного приложения» · 5 откл. · 2026-11-22T23:20:31+03:00` | Работает |
| Weblancer | ✅ | HTTP 200, **20 проектов**, возраст 0.3–83.9 ч, отклики есть. Пример: `1270706 · «Качать приложения с iPhone и писать отзывы» · 1 откл.` | Работает |
| Freelance.ru | ✅ | HTTP 200. `q=сайт`: на странице 25 карточек, распарсено **4**; `q=бот`: **22 из 25**; без запроса: 25 из 25. У каждого лида текст кончается «Гонорар: Гонорар» вместо суммы. Свежих (≤3 ч) на всех трёх страницах — 0 | Работает частично (находки 2, 3) |
| FL.ru RSS | ✅ | HTTP 200, **60 заказов**, возраст 1.7–35 ч. Пример заголовка: `Автоматизация пабг мобайл (Бюджет: 15 000 &#8381;)`. При поясе сервера UTC свежих 1 из 60, при `Europe/Moscow` — **0 из 60** | Работает только при TZ=UTC (находка 1) |
| Threads без логина | ✅ «15–25 свежих постов на фразу» | HTTP 200 (редирект на threads.com). Проверено 10 из 12 фраз по умолчанию, посты дала одна: «нужен сайт» → **18 постов возрастом 399–17 307 ч** (медиана 2138 ч), не по теме («А где wlw девушки вообще знакомятся?», «ищу не раскрученных моделей для своего бренда»). Остальные 9 фраз → 0 постов. Через конвейер прошло **0 из 18** | Не подтверждается (находка 4) |
| Яндекс.Карты без API | ✅ | HTTP 200 (редирект на yandex.com/maps), «стоматология Тула» → **24 организации** с сайтом и телефонами; без сайта — 0 | Работает |
| Аудит сайтов | ✅ | 12 сайтов из выдачи Яндекса за 20 с: 11 → без замечаний, `ama-clinic.ru` → «сайт не открывается (CertificateVerifyError)» (curl подтверждает самоподписанный сертификат). Контроль: `example.com` → «не адаптирован под телефон, почти пустая страница», `httpbin.org/status/404` → «сайт отдаёт ошибку 404», несуществующий домен → `DNSError` | Работает |
| hh.ru | «блокирует IP дата-центров» | UA по умолчанию → **HTTP 400** `bad_user_agent: blacklisted`; с другим UA → HTTP 403 `forbidden` (блок по IP) | Не работает с настройками по умолчанию (находка 5) |
| Поисковики (DuckDuckGo, `ddgs` 9.16.1) | «медленно» | `site:threads.net "нужен сайт"` → 0, `site:threads.com` → 0, `site:vc.ru` → 0, `site:instagram.com` → **1** (reel исполнителя «если вам нужен сайт — пишите в личку») | Технически работает, отдача ≈ 0 (находка 15) |

## 2. Находки

Заготовка для воспроизведения (из корня проекта, в активированном venv). Сохраните как `lh.py` рядом с проектом — на неё ссылаются команды ниже:

```python
# lh.py
import asyncio, os, sys, tempfile, types, logging
os.environ.setdefault("BOT_TOKEN", "123456:TESTDUMMY")
from leadhunter.db import Database
from leadhunter.models import Lead
from leadhunter.pipeline import Pipeline
logging.disable(logging.CRITICAL)
class N:
    def __init__(s): s.sent = []
    async def notify(s, lead_id, lead, info): s.sent.append(info)
async def fresh(llm=None):
    db = Database(os.path.join(tempfile.mkdtemp(), "t.db")); await db.connect()
    n = N(); return db, n, Pipeline(db, n, 5, llm)
_i = [0]
def tg(text, chat="1", title="Общий чат", contact="@client"):
    _i[0] += 1
    return Lead(source="telegram", external_id=f"{chat}:{_i[0]}", text=text, title=f"Чат: {title}", author="А",
                contact=contact, extra={"chat_key": chat, "chat_title": title})
def run(coro): asyncio.run(coro); sys.stdout.flush(); os._exit(0)
```

### Сводная таблица (по серьёзности)

| № | Серьёзность | Файл:строка | Что не так | Как чинить |
|---|---|---|---|---|
| 1 | Критично | `sources/rss_boards.py:28` | `mktime()` читает UTC-время RSS как локальное. На сервере с поясом Москвы все заказы FL.ru «стареют» на 3 ч и целиком отсекаются фильтром свежести (0 из 60) | `calendar.timegm(e.published_parsed)` |
| 4 | Критично | `sources/threads_web.py:10,72` | Поиск Threads без логина не отдаёт свежие посты: 9 из 10 проверенных фраз → 0, одна → 18 постов возрастом от 17 дней до 2 лет; в конвейер не проходит ничего. Источник включён по умолчанию и считается «✅» | Не обещать в README; считать источник рабочим только с `THREADS_ACCESS_TOKEN`; слать алерт, если N проходов подряд дают 0 постов моложе 48 ч |
| 2 | Важно | `sources/freelance_ru.py:12` | `CARD_RE` требует ровно `class="task-card"`, а премиум-карточки имеют `class="task-card task-card--premium"` — они пропускаются (21 из 25 по «сайт») | `r'<article class="task-card[^"]*">'`; премиум при желании помечать в тексте |
| 3 | Важно | `sources/freelance_ru.py:18` | `BUDGET_RE` ловит подпись `task-card__budget-lbl` → в каждом лиде «Гонорар: Гонорар», сумма теряется, `extract_budget` пуст | Матчить `class="task-card__budget"` точно и брать `<span class="bold">` |
| 5 | Важно | `config.py:119-120`, `sources/hh.py:36` | `HH_USER_AGENT` по умолчанию в чёрном списке hh (HTTP 400), а `hh_enabled=True` → источник падает на каждом проходе и на 3-й раз шлёт владельцу алерт «⚠️ hh.ru не работает третий раз подряд: … 400 Bad Request» | По умолчанию `hh_enabled=False`, либо не запускать источник без своего UA |
| 6 | Важно | `pipeline.py:69-79`, `db.py:267-275` | Спам-детект считает любые сообщения автора, а не лиды: 3 обычные реплики в 3 чатах за час → автор навсегда в игноре, его настоящий заказ потом молча отбрасывается | Считать только сообщения, прошедшие скоринг; бан делать временным и показывать в карточке/алерте |
| 7 | Важно | `sources/telegram_chats.py:82`, `models.py:26` | В `title` лида кладётся «Чат: <название>», оно попадает в `full_text` → скоринг, гео, правила и хэш-дедуп работают по названию чата. В чате «Боты и сайты» любое «подскажите, нужен юрист» получает 10/10 | Название чата держать только в `extra["chat_title"]`, в скоринг/хэш отдавать `lead.text` |
| 8 | Важно | `scoring.py:8-11,48-52` | Основы ищутся как префикс: «бот» ловит «ботинки»/«ботокс», «ролик» — «роликовые коньки», «визитк» — печать визиток, «амо» — «аморальный». Без ИИ (режим по умолчанию) это карточки с баллом 5–8 | Сравнивать леммы (pymorphy уже подключён) либо список исключений; короткие основы — только целым словом |
| 9 | Важно | `loops.py:28-29`, `loops.py:44-45`, `bot/notify.py:136-145` | `notifier.send()` глотает ошибку отправки, а цикл всё равно ставит `sla_pinged=1` / очищает `followup_at` → при сбое Telegram SLA-пинг и follow-up теряются навсегда | `send()` должен возвращать успех; помечать только после успешной отправки |
| 10 | Важно | `db.py:184-190`, `loops.py:21` | SLA не смотрит, была ли карточка: пингуются лиды, сохранённые «тихо» (сверх 5 за проход, пауза), и все старые после простоя — 40 лидов → 40 пингов «никто не взял 1800 мин» за минуту, включая протухшие заказы бирж | В `sla_overdue` добавить `msg_id IS NOT NULL`, верхнюю границу возраста и проверку `paused` |
| 11 | Важно | `bot/notify.py:165-185`, `bot/handlers.py:112` | Если карточку не удалось отправить, лид остаётся в базе с `msg_id NULL` и досылается только по `/start`; сам лид уже помечен `seen` и повторно не придёт | Фоновая досылка `undelivered_leads()` раз в несколько минут |
| 12 | Важно | `pipeline.py:124,143-144`, `sources/yandex_maps_web.py:41,55` | У лидов с карт фиксированный балл 6 (нет сайта) и 5 (аудит), он сравнивается с общим порогом: при `/threshold 6` пропадают все лиды аудита, при 7 — все лиды Яндекс.Карт, без предупреждения | Не применять порог к `preset_score` или сделать отдельный порог для карт |
| 13 | Мелочь | `geo.py:4-39` | Гео-фильтр (выключен по умолчанию): имена и слова считаются городами («Меня зовут Владимир», «грозный дизайн», «угольная шахта», «тур на Бали»); «Питер» ≠ «Санкт-Петербург», «мск»/«спб» не понимаются | Таблица синонимов; города-омонимы учитывать только с предлогом «в/из/г.» |
| 14 | Мелочь | `sources/base.py:19` | `strip_html` знает 5 HTML-сущностей; в FL.ru `&#8381;` остаётся в заголовке, бюджет не извлекается | `html.unescape()` |
| 15 | Мелочь | `pipeline.py:38,87-89`, `sources/web_search.py:40` | Группы `hh.ru` и `web` выключены по умолчанию, но источники всё равно опрашиваются (web — 3 сайта × 12 фраз = 36 запросов к DDG каждые 30 минут), а результаты выбрасываются; DDG-ошибки/блокировки глотаются на уровне debug и неотличимы от пустой выдачи | Не запускать источник, если его группа выключена; ошибки DDG считать сбоем |
| 16 | Мелочь | `dedup.py:27`, `pipeline.py:54` | Семантический дедуп живёт только в памяти: после перезапуска тот же перефразированный заказ приходит второй раз | Хранить шинглы последних 48 ч в базе или прогревать индекс из `leads` при старте |
| 17 | Мелочь | `llm.py:101` | `bool(data.get("is_lead"))`: ответ `"false"`/`"нет"` строкой считается лидом (карточка 9/10) | Явно разбирать `true/false/да/нет` |
| 18 | Мелочь | `llm.py:65-78`, `pipeline.py:165-167` | После исчерпания лимита ИИ в час конвейер молча переходит на словарь: реклама, которую ИИ отсеивал, идёт в карточки | Алерт владельцу при достижении лимита; опция «без ИИ не слать» |
| 19 | Мелочь | `db.py:351-362` | CSV-экспорт не экранирует формулы: текст чужого сообщения `=HYPERLINK(...)` попадёт в Excel как формула | Префикс `'` для ячеек, начинающихся с `= + - @` |
| 20 | Мелочь | `db.py:326` | `undelivered_leads` сравнивает `found_at` как строки; Kwork/Freelance.ru пишут `+03:00`, поэтому заказ возрастом 5 ч считается «моложе 3 ч» | Писать `found_at` в UTC (`astimezone(timezone.utc)`) |
| 21 | Мелочь | `loops.py:52-58`, `main.py:96` | Дайджест: в поясах с переводом часов — дубль (08:00 и 09:00) осенью и опоздание на час весной; неверный `DIGEST_TZ`/`DIGEST_HOUR=24` роняет задачу при старте без сообщения. `Europe/Moscow` не затронут | Считать паузу через UTC; проверять настройки при запуске |
| 22 | Мелочь | `db.py:110-124,267-275` | `seen`, `seen_hashes`, `author_events` никогда не чистятся; запись идёт на каждое сообщение чата (3 commit): 1000 реплик → +3000 строк, +320 КБ | Удалять записи старше N дней раз в сутки |
| 23 | Мелочь | `extract.py:6-9` | Бюджет: «3 к. дома» → `3 к`, «в 2024 р.» → `2024 р.`, «5-7 тыс» → `7 тыс`, «1.5 млн» → пусто. Контакты: `t.me/username` не извлекается | Диапазоны и «млн» в регулярке; `t.me/…` в контакты |
| 24 | Мелочь | `scoring.py:14-18` | Намерение не ловит «нужны», «требуются», «необходим»: «Необходим лендинг для клиники» → 2 балла, ниже порога | Добавить формы или сравнивать по леммам |

### Воспроизведение и фактический вывод

Команды запускаются из корня проекта в активированном venv; там, где указан `python - <<'EOF'`, используется `lh.py` из начала раздела. Вывод — с моих прогонов.

**1. FL.ru RSS и часовой пояс сервера**
```bash
for tz in UTC Europe/Moscow Asia/Novosibirsk; do TZ=$tz python -c "
import urllib.request
from leadhunter.sources import rss_boards
from leadhunter.freshness import check, Freshness
raw = urllib.request.urlopen(urllib.request.Request('https://www.fl.ru/rss/all.xml', headers={'User-Agent': 'Mozilla/5.0'})).read()
leads = rss_boards.parse_feed(raw, 'https://www.fl.ru/rss/all.xml')
print('$tz', max(l.found_at for l in leads).isoformat(), 'свежих:', sum(check(l, Freshness())[0] for l in leads), 'из', len(leads))"; done
```
```
UTC              2026-11-22T19:34:40+00:00  свежих: 1 из 60
Europe/Moscow    2026-11-22T16:34:40+00:00  свежих: 0 из 60
Asia/Novosibirsk 2026-11-22T12:34:40+00:00  свежих: 0 из 60
```
Один и тот же заказ получает разное время. В Docker (UTC) незаметно, при установке без Docker на машину с московским временем FL.ru не даёт ни одного лида.

**2–3. Freelance.ru**
```bash
curl -s -A 'Mozilla/5.0' 'https://freelance.ru/task?q=сайт' -o /tmp/fr.html
grep -c '<article class="task-card' /tmp/fr.html; grep -c '<article class="task-card">' /tmp/fr.html
python -c "
from leadhunter.sources.freelance_ru import parse_freelance_ru_html as p
from leadhunter.extract import extract_budget
l = p(open('/tmp/fr.html').read()); print(len(l), repr(l[0].text[-20:]), repr(extract_budget(l[0].full_text)))"
```
```
25
4
4 'оды\nГонорар: Гонорар' ''
```
В HTML при этом: `<div class="task-card__budget"><span class="bold">3 500 &#8381;</span> <span class="cost-unit">/ заказ</span></div>`. Оговорка: пропущенные карточки помечены «Только для Премиум» — возможно, отклик на них платный, но пропускаются они случайно, а не по решению.

**4. Threads без логина**
```bash
python - <<'EOF'
import asyncio
from datetime import datetime, timezone
from leadhunter.http import Fetcher
from leadhunter.sources.threads_web import parse_threads_html, SEARCH
async def main():
    f = Fetcher()
    for q in ("нужен сайт", "нужен лендинг", "ищу разработчика бота"):
        r = await f.get(SEARCH, params={"q": q, "serp_type": "recent"})
        ages = sorted((datetime.now(timezone.utc) - l.found_at).total_seconds() / 3600 for l in parse_threads_html(r.text))
        print(q, r.status_code, "постов", len(ages), "моложе 48 ч:", sum(a <= 48 for a in ages), "мин. возраст ч:", round(ages[0]) if ages else "-")
asyncio.run(main())
EOF
```
```
нужен сайт 200 постов 18 моложе 48 ч: 0 мин. возраст ч: 399
нужен лендинг 200 постов 0 моложе 48 ч: 0 мин. возраст ч: -
ищу разработчика бота 200 постов 0 моложе 48 ч: 0 мин. возраст ч: -
```
Повторный запрос через несколько минут дал те же 18 постов — это не лимит запросов. Оговорка: проверено с IP дата-центра вне РФ; с домашнего российского IP выдача может отличаться — проверить не удалось.

**5. hh.ru**
```bash
curl -s -A 'LeadHunter/0.1 (contact@example.com)' 'https://api.hh.ru/vacancies?text=сайт&per_page=1'
```
```
{"description":"Bad User-Agent header","errors":[{"value":"blacklisted","type":"bad_user_agent"}],…}
```
Три прохода `HhSource.run()` с настройками по умолчанию → алерт: `⚠️ hh.ru не работает третий раз подряд: HTTPStatusError: Client error '400 Bad Request' …`.

**6, 7, 8, 12, 16. Конвейер**
```bash
python - <<'EOF'
from lh import *
async def main():
    db, n, p = await fresh()
    for k, t in enumerate(["Всем доброе утро, коллеги! Как дела?", "Спасибо большое за совет, очень помогло", "Подскажите, во сколько сегодня созвон?"]):
        await p.handle(tg(t, chat=str(k), title=f"Сообщество {k}", contact="@ivan"))
    print("6:", (await db.get_ignored())["author"], await p.handle(tg("Ищу разработчика, нужен сайт для магазина, бюджет 150к", contact="@ivan")))
    db, n, p = await fresh()
    print("7:", await p.handle(tg("Подскажите, нужен юрист по разводам, срочно", title="Боты и сайты | фриланс")), n.sent[-1].score, n.sent[-1].res.topic)
    for t in ("Нужны ботинки зимние, подскажите где заказать недорого", "Ищу косметолога, нужен ботокс, подскажите", "Ищу роликовые коньки, нужна помощь с выбором"):
        print("8:", await p.handle(tg(t)), n.sent[-1].score, n.sent[-1].res.topic)
    await db.set_setting("threshold", "7")
    print("12:", await p.handle(Lead(source="yandex_maps", external_id="y1", title="Автосервис Мотор", text="Тула, Советская 5. Сайта нет. +7 4872 00-00-00", preset_score=6)))
    await db.set_setting("threshold", "5")
    T = "Нужен веб-сайт для кафе в Самаре, ищу исполнителя, бюджет до 40 тыс, срок месяц, подробности в личные сообщения"
    a = await p.handle(Lead(source="kwork", external_id="k1", title="Сайт для кафе", text=T, intent_implied=True))
    b = await p.handle(Lead(source="weblancer", external_id="w1", title="Разработка сайта кафе", text=T + " 🙏", intent_implied=True))
    c = await Pipeline(db, n, 5).handle(Lead(source="freelance.ru", external_id="f1", title="Кафе: сайт", text=T + " !!", intent_implied=True))
    print("16:", a, b, "после перезапуска:", c)
run(main())
EOF
```
```
6: {'@ivan': 'авто: 3 чатов за час'} False
7: True 10 ['сайт', 'бот']
8: True 8 ['бот']
8: True 8 ['бот']
8: True 8 ['ролик']
12: False
16: True False после перезапуска: True
```

**9–10. SLA и follow-up** (настоящий `TelegramNotifier`, бот — заглушка; один проход цикла)
```bash
python - <<'EOF'
from lh import *
from datetime import datetime, timedelta, timezone
from leadhunter import loops
from leadhunter.scoring import ScoreResult
from leadhunter.bot.notify import TelegramNotifier
class Bot:
    def __init__(s): s.sent, s.down = [], False
    async def send_message(s, chat_id, text, **kw):
        if s.down: raise ConnectionError("нет связи")
        s.sent.append(text); return types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id), message_id=1, message_thread_id=None)
async def once(coro):
    t = asyncio.create_task(coro); await asyncio.sleep(0.5); t.cancel()
async def main():
    for case in ("сбой Telegram", "40 старых лидов без карточки"):
        db, _, _ = await fresh(); bot = Bot(); bot.down = case.startswith("сбой")
        for i in range(1 if bot.down else 40):
            lid = await db.save_lead(Lead(source="kwork", external_id=str(i), title="Нужен сайт", text="т"), 9, ScoreResult(9))
            await db.conn.execute("UPDATE leads SET created_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(), lid))
        await db.conn.commit()
        await once(loops.sla_loop(db, TelegramNotifier(bot, db, [111]), 10, 7))
        print(case, "→ пингов:", len(bot.sent), "| sla_pinged у лида 1:", (await db.get_lead(1))["sla_pinged"])
    db, _, _ = await fresh(); bot = Bot(); bot.down = True
    lid = await db.save_lead(Lead(source="kwork", external_id="f", title="Нужен сайт", text="т"), 9, ScoreResult(9))
    await db.set_status(lid, "contacted"); await db.schedule_followup(lid, 0)
    await once(loops.followup_loop(db, TelegramNotifier(bot, db, [111])))
    print("follow-up при сбое → отправлено:", len(bot.sent), "| followup_at:", (await db.get_lead(lid))["followup_at"])
run(main())
EOF
```
```
сбой Telegram → пингов: 0 | sla_pinged у лида 1: 1
40 старых лидов без карточки → пингов: 40 | sla_pinged у лида 1: 1
follow-up при сбое → отправлено: 0 | followup_at: None
```

**11.** Настоящий `TelegramNotifier` с заглушкой бота, у которой `send_message` бросает `ConnectionError`: `handle → True | msg_id: None | повтор → False | undelivered: 1`. `undelivered_leads()` вызывается только в обработчике `/start` (`bot/handlers.py:112`).

**13, 14, 17, 19, 20, 23, 24. Короткие проверки**
```bash
python - <<'EOF'
from lh import *
import json, httpx
from datetime import datetime, timedelta, timezone
from leadhunter.geo import geo_allows
from leadhunter.sources.base import strip_html
from leadhunter.extract import extract_budget
from leadhunter.scoring import score_text, Keywords, ScoreResult
from leadhunter.llm import LLMClassifier
async def main():
    print("13:", [geo_allows(t, ["Москва"]) for t in ("Меня зовут Владимир, нужен сайт", "Нужен грозный дизайн сайта", "Нужен лендинг про тур на Бали")], geo_allows("нужен сайт, я в Питере", ["Санкт-Петербург"]), geo_allows("нужен сайт в Москве", ["мск"]))
    print("14:", strip_html("Бюджет: 15 000 &#8381;"), repr(extract_budget(strip_html("Бюджет: 15 000 &#8381;"))))
    llm = LLMClassifier("k", "http://x/v1", "m")
    ans = {"is_lead": "false", "score": 9, "summary": "реклама студии"}
    llm.client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ans)}}]})))
    db, n, p = await fresh(llm)
    print("17:", await p.handle(tg("Ищу разработчика, нужен сайт для кафе в центре")), n.sent[-1].score)
    await db.save_lead(Lead(source="telegram", external_id="c", title='=HYPERLINK("http://evil.example";"Открыть")', text="@SUM(1+1)"), 9, ScoreResult(9))
    print("19:", (await db.export_csv()).decode("utf-8-sig").splitlines()[1][-70:])
    now = datetime.now(timezone.utc)
    for name, tz in (("5 ч, +03:00", timezone(timedelta(hours=3))), ("5 ч, UTC", timezone.utc)):
        await db.save_lead(Lead(source="kwork", external_id=name, title=name, text="т", found_at=(now - timedelta(hours=5)).astimezone(tz)), 9, ScoreResult(9))
    print("20:", [r["title"] for r in await db.undelivered_leads(20) if r["source"] == "kwork"])
    print("23:", [extract_budget(t) for t in ("квартира в 2 комнаты, 3 к. дома", "в 2024 р. мы открылись", "нужен сайт за 5-7 тыс", "от 1.5 млн")])
    print("24:", [score_text(t, Keywords()).score for t in ("Необходим лендинг для клиники", "Требуются программисты: сделать сайт")])
run(main())
EOF
```
```
13: [False, False, False] False False
14: Бюджет: 15 000 &#8381; ''
17: True 9
19: ,new,,,"=HYPERLINK(""http://evil.example"";""Открыть"")",@SUM(1+1),,,,
20: ['5 ч, +03:00']
23: ['3 к', '2024 р.', '7 тыс', '']
24: [2, 2]
```

**15.** `site:threads.net "нужен сайт"`, `site:threads.com …`, `site:vc.ru …` через `DDGS().text(q, region="ru-ru", timelimit="w", max_results=20)` → `DDGSException: No results found` (в коде превращается в пустой список); `site:instagram.com` → 1 результат. В конвейере `Lead(source="web:vc.ru", …)` и `Lead(source="hh.ru", …)` при настройках по умолчанию → `False` (группы `leads_web`/`leads_jobs` = «0»), при этом `build_sources` оба источника запускает.

**18.** `LLMClassifier(..., max_per_hour=1)`: первый текст «Нужен сайт? Ищу клиентов, срочно, бюджет любой» (ИИ: не лид) → `False`; второй такой же после исчерпания лимита → `True`, балл 8 по словарю.

**21.** Цикл `digest_loop` с подменёнными часами. `Europe/Moscow`, 9:00 → срабатывания `2026-11-23 09:00`, `2026-11-24 09:00` (норма). `Europe/Berlin`: `2026-10-25 08:00 CET` и снова `2026-10-25 09:00 CET`; весной — `2026-03-29 10:00 CEST`. `digest_loop(db, n, 9, "Moscow")` → `ZoneInfoNotFoundError`, `hour=24` → `ValueError: hour must be in 0..23`.

**22.** 1000 обычных реплик одного чата через `Pipeline.handle` (3.3 с): `seen` +1000, `seen_hashes` +1000, `author_events` +1000, файл базы 100 → 420 КБ; `DELETE` для этих таблиц в `db.py` нет.

## 3. Проверено и работает

- `pytest`: 211 passed.
- Kwork, Weblancer, Яндекс.Карты без API, аудит сайтов — живые запросы, данные полные (см. раздел 1); даты у всех источников с таймзоной.
- Конвейер: базовый путь (балл, услуга, бюджет, контакты), пустой и короткий текст, текст 1.7 МБ (2.8 с, без падения), эмодзи, дата без таймзоны (считается UTC), повтор по id, повтор текста, перефраз с другой биржи (семантический дедуп), игнор-листы чатов и авторов (без учёта регистра), пауза (сохраняет без карточки), выключенная группа источников не помечает лид просмотренным.
- Свежесть: старше 3 ч и больше 15 откликов отсекаются, бонусы за свежесть считаются, для Telegram действует общий предел 48 ч.
- Правила: лемматизация («сайты», «ищем лендинги»), `"точная фраза"`, `+(а|б)`, блок-правила, битые выражения пропускаются без падения; правило поднимает балл до порога.
- ИИ-ветка с заглушкой: нормальный вердикт, отказ, ответ в блоке `json` с тройными кавычками, обрезка текста до 4000 символов, балл вне 0–10, переход на запасную модель при 429, откат на словарь при HTTP 500 / битом JSON / `score="8/10"`; при балле ниже 2 ИИ не вызывается.
- Одновременные вызовы: 30 одновременных `handle()` одного лида → ровно одна карточка; 200 разных лидов одновременно → 200 строк, без исключений; 20 одновременных «Беру» → один победитель и одно событие; два соединения к одному файлу × 400 записей → без ошибок блокировки.
- База: копия `data/leads.db` из архива открывается, `integrity_check = ok` до и после, данные целы, повторный `connect()` ничего не меняет; миграция со старой схемы (таблица `leads` без 14 новых колонок, 50 строк) проходит, пользовательские ключевые слова не затираются.
- SQL-инъекции: все запросы параметризованы, строка `x'); DROP TABLE leads;--` во всех полях и настройках вреда не наносит; `funnel(by=…)` берёт столбец из фиксированного словаря (на мусоре — `KeyError`).
- Циклы: SLA пингует ровно один раз и только лиды `new` с баллом ≥ 7 старше 10 минут; follow-up шлёт один раз и только при статусе `contacted`; дайджест в `Europe/Moscow` приходит раз в сутки, в понедельник с воронкой; исключение внутри прохода цикл не роняет.
- В архивной базе лежат 2 «лида» `web:vc.ru` с баллами 9 и 7 — это статьи («Сколько стоит разработка чат-бота…», «Агрегаторы нейросетей в России…»), а не заказы.

## 4. Не удалось проверить и почему

- **Threads и hh.ru с российского домашнего IP** — машина проверки в дата-центре вне РФ. Для hh это меняет только второй ответ (403 по IP); отказ 400 из-за User-Agent от IP не зависит. Для Threads результат может отличаться.
- **Telegram-чаты (Telethon), VK, Instagram, официальные API Threads/Яндекса, Serper** — нужны ключи/сессии, которых нет; разбор сообщений Telegram проверен только на уровне конвейера с синтетическими лидами.
- **Браузерные источники (`leadhunter/browser/*`, Instagram web)** — не запускались: требуют профиль с логином.
- **Настоящая отправка в Telegram** — намеренно не выполнялась, чтобы не трогать рабочего бота; поведение при сбоях отправки проверено на заглушке бота с настоящим `TelegramNotifier`.
- **`discovery.py`, `chat_catalog.py`, `niches.py`, `phrases.py`** — запуском не проверялись (поиск и оценка чатов требуют Telethon-сессии); по ним находок нет.
- **Капча/бан при длительной работе** — каждый сайт вызывался 1–2 раза, поведение при частых запросах и работа через прокси не проверялись.
- **Freelance.ru: доступность премиум-заказов для обычного аккаунта** — без аккаунта не проверить, нужно ли их показывать.
- **Смешанные даты с таймзоной и без** в `sources/base.py:51` дают `TypeError` при сортировке, но ни один текущий источник наивных дат не создаёт — на реальных данных не воспроизводится.
