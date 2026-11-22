# LeadHunter — проверка контент-фабрики и установки

Область: `leadhunter/content/*`, `leadhunter/bot/content.py` (как вызывающая сторона), `install.sh`, `update.sh`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `pyproject.toml`, `README.md`.

**Как проверялось.** Архив распакован, код проекта не менялся. Python 3.12.16, `pip install -e ".[dev]"`, существующие тесты: `211 passed in 11.72s`. Машина: 2 vCPU Xeon, ffmpeg и Docker 29.9.2 есть. Настоящий `BOT_TOKEN` не использовался: бот гонялся через подменённую сессию aiogram из `tests/test_content.py::make_env`, внешние API (LLM, Gemini, VK, Threads, Instagram, YouTube) — через `httpx.MockTransport`, установщик — в чистом контейнере `ubuntu:24.04` с фиктивным токеном.

**Как повторить.** Проверочные скрипты лежат в приложенном `checks.zip` (там же логи их запусков `*.log` и логи установки):

```bash
python3.12 -m venv venv && . venv/bin/activate && pip install -e "<проект>[dev]"
LH_SRC=<путь к проекту> LH_OUT=./out python checks/<скрипт>.py
```

Ниже «команда» — это имя скрипта и раздел его вывода; «вывод» — фактические строки из лога.

## Сводная таблица

| № | Серьёзность | Файл:строка | Что не так | Как чинить |
|---|---|---|---|---|
| 1 | критично | `.env.example:3,12,35,36,58,62,64,68,72,83,97–100,122,124`; `docker-compose.yml:4` | В Docker-режиме (путь установщика по умолчанию) 16 пустых переменных получают значением текст комментария; бот не стартует | Комментарии — отдельной строкой над переменной |
| 2 | критично | `.env.example:32`; `leadhunter/config.py:23` | `TG_API_ID=` (пусто) → `ValidationError`, бот не стартует после `install.sh` в любом режиме | `env_ignore_empty=True` или закомментировать строку |
| 3 | критично | `content/factory.py:202–221`; `content/scheduler.py:19–25`; `bot/content.py:524–533`; `content/store.py:99` | Нет защиты от повторной публикации: двойной тап, повтор после частичного успеха, два процесса, перезапуск посреди публикации, повторное расписание → дубли в канале | Атомарно «захватывать» запись, пропускать уже опубликованные площадки |
| 4 | важно | `bot/content.py:291`; `content/store.py:71`; `content/scheduler.py:19,57` | Идеи из `/plan` получают `status=scheduled`: планировщик пытается их «опубликовать» и сбрасывает, автопилот их не видит (README обещает обратное) | Не ставить идеям `scheduled`; искать их в автопилоте по `kind="idea"` |
| 5 | важно | `install.sh`, `update.sh` (все файлы архива) | В архиве все 97 текстовых файлов с CRLF и без бита `+x`: `install.sh` и `update.sh` не запускаются | `.gitattributes`: `*.sh text eol=lf`; `git update-index --chmod=+x` |
| 6 | важно | `install.sh:249` | На чистой Ubuntu без `curl` установщик падает (exit 127); при `LEADHUNTER_YES=1` Docker ставится принудительно, Python-режим выбрать нельзя | Проверять/ставить `curl`; не ронять скрипт при неудаче Docker; переменная `LEADHUNTER_NO_DOCKER` |
| 7 | важно | `install.sh:208–226` | `systemctl` есть, но systemd не запущен (WSL, контейнер) → exit 1 без «Готово» и без команды запуска | Проверять `[ -d /run/systemd/system ]` |
| 8 | важно | `update.sh:24` | От root без `sudo` (типичный VPS): код обновлён, служба не перезапущена, exit 127 | Как в `install.sh`: `SUDO=""` для root |
| 9 | важно | `README.md:76,79`; `install.sh:3,7` | Команда установки `curl …/install.sh \| bash` отдаёт 404 | Сделать репозиторий публичным или поправить адрес |
| 10 | важно | `content/publish.py:125–135` | Длинный пост в Telegram: обложка ушла, «хвост» упал → считается ошибкой, повтор шлёт обложку второй раз | Возвращать успех с `note`, если первое сообщение ушло |
| 11 | важно | `content/scheduler.py:55,73–79` | Ошибка ИИ в автопилоте: день отмечен выполненным, черновика нет, владельцу ничего не сообщается | Сообщать владельцу; отметку ставить после успеха с ограничением попыток |
| 12 | важно | `content/scheduler.py:19`; `content/store.py:108` | После простоя все пропущенные слоты выходят одной пачкой за секунду | Публиковать по одному за проход или переносить просроченные |
| 13 | важно | `bot/content.py:429–447`; `content/render.py:81–83` | `/brand` принимает любой «цвет» (`#fff`, `#red`); после этого `/publish`, `/carousel`, `/reel` не рендерятся | Проверять `^#[0-9a-fA-F]{6}$` при вводе |
| 14 | важно | `content/factory.py:74,81`; `content/toon.py:87`; `bot/content.py:95` | Неожиданные типы в ответе ИИ: `title: null`, `body` списком → сырая ошибка; реплика мультика строкой → необработанное исключение, карточка и `/show` падают | Приводить типы в `_clean`; `e(who or "")` |
| 15 | важно | `content/factory.py:41–43`; `bot/content.py:479–480` | Правка лид-магнита через «✏️ Текст» не меняет текст, который уйдёт в канал | Писать правку в `data["post"]` или брать `caption` первым |
| 16 | важно | `content/factory.py:157,202–220` | Сборка видео упала → на диске остаётся пустой `.mp4`; такой ролик публикуется голым текстом и получает `published` | Удалять файл при ошибке; не публиковать видео-материал без видео |
| 17 | важно | `content/gemini.py:127–147`; `content/factory.py:139,154` | Минутный лимит TTS: ролик из 8 сцен ждёт 16 минут и выходит без звука с пометкой «видео собрано» | После первого 429 отключать озвучку до конца сборки и предупреждать |
| 18 | важно | `content/render.py:8–12` | Эмодзи на слайдах, обложках и в роликах — пустые квадраты | Убирать эмодзи перед рендером или добавить emoji-шрифт |
| 19 | важно (вживую не подтверждено) | `content/publish.py:244,311`; `content/media.py:30` | В Threads/Instagram PNG отдаётся по ссылке `.jpg` | Перекодировать в JPEG перед `public_url` |
| 20 | важно (реакция Telegram не проверялась) | `bot/content.py:139` | Обрезка карточки `text[:4000]` рвёт HTML-тег или сущность | Резать по строкам до лимита |
| 21 | важно | `content/media.py:30–33`; `content/publish.py:244,311` | Каждая публикация в Threads/Instagram оставляет копию медиа в `data/content` навсегда | Удалять временные копии после публикации |
| 22 | мелочь | `content/factory.py:216`; `bot/content.py:187` | Таймаут даёт пустую ошибку: «YouTube: », «Не получилось: » | Подставлять имя исключения, если текст пуст |
| 23 | мелочь | `leadhunter/llm.py:94`; `bot/content.py:187` | Ошибки ИИ показываются сырым текстом Python | Понятные сообщения, один повтор на не-JSON |
| 24 | мелочь | `content/generator.py:146–149,160` | `null` в слайде → на картинке слово «None»; хэштеги `#сайт, #бот`, `#None` | `str(x or "")`; делить по запятым и пробелам |
| 25 | мелочь | `content/generator.py:142`; `content/factory.py:74` | Пустой ответ ИИ `{}` сохраняется как пустой черновик; карусель без слайдов — без предупреждения | Проверять обязательные поля |
| 26 | мелочь | `content/publish.py:17` | Threads молча режет текст до 500 символов | Добавлять `note` |
| 27 | мелочь | `bot/content.py:338` | `/schedule` принимает прошедшую дату — материал выходит сразу | Отклонять прошедшее время |
| 28 | мелочь | `content/render.py:172`; `content/factory.py:134` | Обложка поста 1080×1080; при `/publish` первая строка текста дублируется на обложке | Размер по площадке; не брать подзаголовок из заголовка |
| 29 | мелочь | `content/toon.py:458,461,525` | `hash()` строк: цвет волос героя меняется между перезапусками (README: «одни и те же герои») | `zlib.crc32` вместо `hash` |
| 30 | мелочь | `bot/content.py:238` | `/toon` без ИИ: 4-й и 5-й герои не появляются в кадре | Ставить в сцену говорящего |
| 31 | мелочь | `README.md:195` | «20 секунд рендерятся ≈ 3 с» — на 2 vCPU 5,0 с; 60 с — 15,4 с | Поправить цифру |
| 32 | мелочь | `update.sh:7–10` | Любая ошибка `git pull` (нет сети, папка не git) → «в папке есть ваши правки кода» | Разные сообщения по причине |
| 33 | мелочь | `.env.example`; `.gitignore`; `install.sh:262` | 6 переменных нет в `.env.example`; `bin/` и `build/` не в `.gitignore`; «Готово: напишите боту /start» печатается, когда бот не запущен | См. ниже |

## Подробности

### 1. Docker-режим: комментарии из `.env.example` становятся значениями (критично)

`install.sh` копирует `.env.example` в `.env` и запускает `docker compose up`. Compose передаёт строку `KEY=      # комментарий` как значение `# комментарий`.

```bash
cp .env.example .env   # + BOT_TOKEN=<фиктивный>
docker compose run --rm --no-deps leadhunter python -c "from leadhunter.config import Settings; Settings()"
```

```text
2 validation errors for Settings
owner_ids
  Value error, invalid literal for int() with base 10: '# ваш Telegram id через запятую; пусто = владелец тот' ...
tg_api_id
  Input should be a valid integer, unable to parse string as an int
```

Значения в контейнере (Compose v5.6.1): `OWNER_IDS`, `PROXY_URL`, `TG_CHATS`, `TG_JOIN_CHATS`, `MINIAPP_URL`, `THREADS_ACCESS_TOKEN`, `IG_ACCESS_TOKEN`, `YANDEX_GEOSEARCH_KEY`, `VK_TOKEN`, `SERPER_API_KEY`, `GEMINI_MODEL`, `GEMINI_IMAGE_MODEL`, `GEMINI_VIDEO_MODEL`, `GEMINI_TTS_MODEL`, `THREADS_USER_ID`, `YT_CLIENT_ID` — все равны тексту комментария, например `PROXY_URL='# http://user:pass@host:port — резидентный/мобильный прокси;…'`. Даже если убрать `OWNER_IDS`, бот получит «прокси», «токен VK» и «модель Gemini» из комментариев.

**Чинить:** в `.env.example` перенести комментарии на отдельную строку над переменной.

### 2. `TG_API_ID=` пустой → бот не стартует (критично)

```bash
cp .env.example .env   # + BOT_TOKEN=<фиктивный>
python -c "from leadhunter.config import Settings; Settings()"
```

```text
1 validation error for Settings
tg_api_id
  Input should be a valid integer, unable to parse string as an integer [type=int_parsing, input_value='', input_type=str]
```

То же в контейнере после полного прогона `install.sh`: `.venv/bin/python -m leadhunter` → эта трасса, `exit=1`. Это единственная переменная, на которой падает разбор (остальные пустые читаются как `''`).

**Чинить:** `SettingsConfigDict(..., env_ignore_empty=True)` либо `# TG_API_ID=` в `.env.example`.

### 3. Повторная публикация (критично)

Команда: `python checks/sched1.py`, разделы B–F. `ContentFactory.publish()` не смотрит на статус записи и на уже опубликованные площадки; `publish_due()` не резервирует запись перед публикацией.

```text
=== B. Двойной тап ✅ Опубликовать / старая карточка -> повторная публикация ===
вызовов publish: 3 | status: published | published: [{"platform": "telegram", ...post/2"}, {"platform": "telegram", ...post/3"}]
=== C. Частичный успех: TG ок, VK упал -> status=published; повтор шлёт в TG второй раз ===
1-й проход: results ['telegram'] errors ['VK: VK: токен протух'] | status: published
2-й проход (VK починили): TG вызовов 2 VK вызовов 2
=== D. Два прохода планировщика одновременно ===
publish_due x2 -> [1, 1] | вызовов publish: 2
=== E. Перезапуск посреди публикации (TG уже ушёл, YouTube грузится) ===
после «убийства»: status scheduled | TG вызовов 1
после рестарта: TG вызовов 2
=== F ===
уже опубликованный снова в расписание: ['#1 выйдет 15.01 10:00'] -> scheduled
publish_due -> 1 | TG вызовов всего 2
```

**Чинить:** перед публикацией `UPDATE content SET status='publishing' WHERE id=? AND status IN ('draft','scheduled')` и продолжать только при `rowcount == 1`; результат каждой площадки записывать сразу; при повторе пропускать площадки из `published`; в `/schedule` и кнопке ✅ отказывать для `published` без явного подтверждения.

### 4. `/plan` не связан с автопилотом (важно)

Команда: `python checks/sched1.py`, раздел A.

```text
idea 1 status= scheduled scheduled_at= 2026-11-24T10:00:00+03:00 platforms= post
publish_due -> 1 | ушло в канал: []
notify: ⚠️ #1 не опубликовался, вернул в черновики:
idea 1 status= draft published= None
autopilot создал: post | topic: Почему сайт не приносит заявки | parent_id: None | лишних вызовов ИИ за идеей: 1
```

В час плана планировщик берёт идею как готовый материал, ничего не публикует (в `platforms` у идеи лежит тип `post`/`reel`, а не площадка), присылает владельцу предупреждение с пустым списком причин и сбрасывает дату. Автопилот ищет `status="draft"` с прошедшей датой, поэтому идею плана не находит ни до, ни после и тратит запрос к ИИ на новую.

**Чинить:** идеям оставлять `status="draft"` (в `store.add` не ставить `scheduled` для `kind="idea"`), в `due()` исключить `kind='idea'`.

### 5. CRLF и нет бита исполнения (важно)

```bash
bash install.sh      # архив как есть, ubuntu:24.04
./install.sh
```

```text
install.sh: line 5: set: pipefail^M: invalid option name      (exit 2)
bash: line 1: ./install.sh: Permission denied                (exit 126)
```

`shellcheck` на файлах из архива: SC1017 на каждой строке. В архиве нет `.gitattributes`. Если в репозитории на GitHub окончания LF, проблема касается только архива и клонов на Windows — проверить не удалось (репозиторий недоступен, см. №9). Побочный эффект CRLF: `get_env GEMINI_API_KEY` возвращает `\r`, и установщик не спрашивает ключ Gemini.

На копии с LF: `shellcheck install.sh update.sh` — 0 замечаний; `shellcheck -o all` — только стиль (SC2250 ×83, SC2292 ×42, SC2310 ×19, SC2312 ×7, SC2249 ×1).

### 6. `install.sh` без `curl` (важно)

```bash
docker run ubuntu:24.04 ... BOT_TOKEN=<фиктивный> LEADHUNTER_YES=1 LEADHUNTER_SKIP_NET=1 bash install.sh
```

```text
  ✅ .env готов (права 600, ключи не показываются)
install.sh: line 249: curl: command not found
INSTALL_EXIT=127
```

Сценарий — `git clone` + `./install.sh` на минимальной Ubuntu/Debian. Из-за `set -euo pipefail` любая неудача `get.docker.com` тоже завершает скрипт, до Python-режима он не доходит.

### 7. `install.sh`: systemd не запущен (важно)

Тот же запуск с установленным `curl`: Docker ставится, демон не отвечает, Python-режим проходит полностью за 1 мин 19 с (Python 3.12.3, ffmpeg, шрифты, `.venv`, `pip install .`, `cloudflared` в `./bin`), затем:

```text
  ✅ cloudflared скачан в ./bin
System has not been booted with systemd as init system (PID 1). Can't operate.
Failed to connect to bus: Host is down
INSTALL_EXIT=1
```

Юнит `/etc/systemd/system/leadhunter.service` записан, «Готово» и команда ручного запуска не напечатаны.

### 8. `update.sh` от root без `sudo` (важно)

Команда: контейнер после установки, root, `sudo` нет, `systemctl` подменён заглушкой.

```text
Updating e7ec1a0..a634d08
Fast-forward
update.sh: line 24: sudo: command not found
exit=127
```

Код и пакет обновлены, служба продолжает работать на старом коде.

### 9. Команда установки из README отдаёт 404 (важно)

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://raw.githubusercontent.com/Stas5252/aiiiiiiiiiiiiiiiiiii/main/install.sh
```

```text
404
```

Репозиторий приватный или адрес другой; `curl … | bash` и `git clone` из README посторонний человек выполнить не сможет.

### 10. Telegram: «хвост» длинного поста (важно)

Команда: `python checks/pub1.py`, раздел Telegram.

```text
пост 1800 симв. с обложкой: OK   SendPhoto caption_len=1021   SendMessage text_len=774
хвост упал: results [] errors ['Telegram -1001234567890: Telegram: Too Many Requests: retry after 30'] | ушло в канал: ['SendPhoto'] | status: draft
повтор владельцем: в канал ещё раз: ['SendPhoto', 'SendMessage']
```

### 11. Автопилот: ошибка ИИ съедает день (важно)

Команда: `python checks/sched1.py`, раздел H.

```text
попытка 1 -> исключение: HTTP 429 Too Many Requests
попытка 2 -> None
autopilot_last = 2026-11-23 | материалов: 0
```

Отметка до генерации поставлена намеренно (комментарий в строке 55), но `autopilot_loop` только пишет в лог — владелец не узнаёт, что черновика сегодня не будет. Час и пояс работают верно: `09:59 -> None`, `10:00 -> 1`, `10:10 повтор -> None`.

### 12. Пропущенные слоты выходят пачкой (важно)

Команда: `python checks/sched1.py`, раздел G.

```text
publish_due -> 3 за 0.00s | в канал подряд: ['Пост 1 текст', 'Пост 2 текст', 'Пост 3 текст']
```

### 13. `/brand` с неверным цветом ломает рендер (важно)

Команда: `python checks/misc1.py`, раздел 1.

```text
/brand Пиксель | @pixel | #fff | #red -> ['Сохранил — новые слайды и обложки будут с этим брендом.']
   /publish -> 'Не получилось: unknown color specifier: '#red''
   /carousel -> 'Не получилось: unknown color specifier: '#red''
   /reel -> '... ⚠️ видео: unknown color specifier: '#red''
   материалов: 4
```

Пустые черновики при этом остаются в базе. `/toon` бренд не использует и собирается.

### 14. Неожиданные типы в ответе ИИ (важно)

Команды: `python checks/ai1.py` (раздел нормализации), `python checks/ai3.py`.

```text
title=null   -> Не получилось: 'NoneType' object is not subscriptable
body списком -> Не получилось: Error binding parameter 4: type 'list' is not supported
/toon тема -> необработанное AttributeError: 'NoneType' object has no attribute 'replace'
/show 1    -> необработанное AttributeError: 'NoneType' object has no attribute 'replace'
материал в базе: toon draft {"video": "….mp4"}
```

В последнем случае ИИ вернул `"line": "привет"` строкой: `normalize_script` ставит `speaker=None`, ролик собирается, но `format_card` падает на `html.escape(None)` — карточка не приходит ни сразу, ни по `/show`.

### 15. Правка лид-магнита не попадает в публикацию (важно)

Команда: `python checks/misc1.py`, раздел 2.

```text
до правки publish-текст:    Забирайте чек-лист — напишите «чек-лист»
Принял. Запомнил, как вы правите — дальше буду писать ближе к этому.
после правки publish-текст: Забирайте чек-лист — напишите «чек-лист»   | caption: МОЙ НОВЫЙ АНОНС от владельца
```

### 16. Упавшая сборка видео (важно)

Команда: `python checks/misc1.py`, раздел 3.

```text
сборка упала: media = {"error": "ffmpeg не найден"} | пустых .mp4 на диске: ['TH95Ov7_arf6eScB.mp4']
публикация ролика без видео: [('a', 0, None)] | status: published
```

В канал уходит одна подпись без видео, материал помечается опубликованным.

### 17. Лимит озвучки Gemini (важно)

Команда: `python checks/gem1.py` (ожидание `asyncio.sleep` подменено счётчиком).

```text
speech: минутный лимит: GeminiError: Gemini 429 ... Please retry in 58s   попыток: 3 | ждал, c: [60.0, 60.0]
запросов к TTS: 24, суммарное ожидание: 960 c = 16.0 мин; media={"video": "….mp4"}
карточка: ... 🎬 видео собрано
```

Ожидание повторяется для каждой сцены; пользователь 16 минут видит «пишу…», а ролик выходит немым без предупреждения.

### 18. Эмодзи — пустые квадраты (важно)

Команда: `python checks/render1.py`; кадры: `sheet_carousel.png`, `sheet_reel.png`, `sheet_edge.png`. Шрифт выбран `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf`, глифов эмодзи в нём нет; `🚀 🔥 💅 😱 👍` рисуются квадратами. ИИ-сценарии эмодзи используют постоянно.

### 19. PNG под именем `.jpg` в Threads/Instagram (важно, вживую не подтверждено)

Команда: `python checks/pub1.py`, разделы Threads и Instagram.

```text
image_url: https://bot.example.com/media/BqV9xxdKT9JjFa1a.jpg | первые байты файла: b'\x89PNG\r\n\x1a\n' | формат: PNG
```

Слайды карусели и шаблонные обложки — PNG. Как Meta реагирует на PNG по ссылке `.jpg`, не проверялось (нет токенов).

### 20. Обрезка карточки рвёт HTML (важно, реакция Telegram не проверялась)

Команда: `python checks/ai1.py`, раздел длинных текстов.

```text
[магнит 15 пунктов] карточка 4001 симв., <b> открыто 15, закрыто 15
обрезка text[:4000] попала внутрь тега/сущности в 19 из 120 вариантов длины заголовка
```

Настоящий Bot API на такой текст отвечает ошибкой разбора — карточка длинного материала может не прийти; на подменённой сессии это не видно.

### 21. Копии медиа копятся (важно)

Команда: `python checks/pub1.py`, последний раздел.

```text
3 публикации одного 5-МБ ролика в Threads+Instagram -> файлов: 6 | МБ: 30
```

### 22–33. Мелочи

- **22.** `python checks/sched1.py`, раздел I: `errors: ['YouTube: ']`; `python checks/ai1.py`: таймаут ИИ → `Не получилось: ` (пусто).
- **23.** `python checks/ai1.py`: не-JSON → `Expecting value: line 1 column 1 (char 0)`; `content: null` → `'NoneType' object has no attribute 'strip'`; `choices: []` → `list index out of range`; JSON-массив → `'list' object has no attribute 'get'`; текст перед JSON не разбирается. Повтора запроса нет.
- **24.** `python checks/ai1.py`: `title: null` в слайде → на слайде и в карточке слово `None`; хэштеги строкой `"сайт, бот, бизнес"` → `#сайт, #бот, #бизнес`; `[null, 5]` → `#None #5`.
- **25.** `python checks/ai1.py`: `{}` → пустой пост-черновик; карусель без `slides` или со `slides` строкой → черновик без картинок, без предупреждения.
- **26.** `python checks/pub1.py`: `текст 800 симв. -> ушло символов: 500 | note в результате нет`.
- **27.** `python checks/sched1.py`, раздел F: `прошлое: ['#1 выйдет 15.01 10:00'] -> scheduled 2025-01-15T07:00:00+00:00`, следом `publish_due -> 1`.
- **28.** `python checks/render1.py`: обложка поста `(1080, 1080)`; кадр `post_1.png` — заголовок и первая строка поста совпадают.
- **29.** `python checks/misc1.py`, раздел 4: `md5 одного и того же кадра в 3 запусках: ['e9fede9cc3', 'a89867ced1', '7862495a39']`; кадр `sheet_toon_seed.png` — у «Разработчика» разный цвет волос.
- **30.** `python checks/ai2.py`: `/toon Название | А: раз | Б: два | В: три | Г: четыре | Д: пять`; кадр `sheet_toon5.png` — в кадре только А, Б, В; реплики Г и Д идут субтитром, никто не говорит.
- **31.** `python checks/render2.py`: `toon scenes=5 dur_each=4.0 -> video 20.0s, render 4.98s`; `scenes=10 dur_each=6.0 -> video 60.0s, render 15.36s`.
- **32.** `bash update.sh` в папке из архива: `fatal: not a git repository` → `❌ Не удалось обновить: в папке есть ваши правки кода. Отмените их (git stash)…`; то же при недоступной сети.
- **33.** В `Settings` есть, в `.env.example` нет: `FREELANCE_RU_INTERVAL`, `HH_INTERVAL`, `KWORK_INTERVAL`, `TG_SEARCH_WORDS`, `WEBLANCER_INTERVAL`, `WEBLANCER_URLS`. После установки `git status`: `?? bin/`, `?? build/`. В Python-режиме без systemd установщик печатает «Готово: напишите боту /start» раньше команды запуска — бот в этот момент не работает (README: «…и запустит бота»).

## Проверено и работает

- Существующие тесты: `211 passed`.
- Рендер без ИИ: `/carousel` — 5 PNG 1080×1350 за 0,13 с; `/reel` — MP4 1080×1920, 30 кадров/с, 12 с, за 1,7 с; `/toon` — MP4 1080×1920, 24 кадра/с, 14 с, звук AAC, за 3,4 с; обложка поста — PNG за 0,03 с.
- Кириллица читается; длинные слова и длинные тексты переносятся и обрезаются многоточием, за границы слайда и кадра текст не выходит (`sheet_edge.png`).
- Ролик из 10 сцен на 20 с собирается за 3,0 с. `/reel` без озвучки даёт файл без аудиодорожки (только видеопоток) — это ожидаемо, но стоит знать.
- ИИ на моках: ответ в ` ```json ` разбирается; 429 с запасной моделью — повтор срабатывает; длительность сцен приводится к 1,5–8 с; лимиты (10 слайдов, 10 сцен, 15 пунктов, 10 хэштегов) соблюдаются; ошибки ИИ не роняют бота (кроме №14).
- Обучение на правках: пара «было → стало» попадает в системный промпт следующей генерации, берутся последние 4, повтор того же текста не дублируется.
- Gemini на моках: 429, пустой ответ, отказ текстом, `status=failed`, зависшее видео → понятная `GeminiError`; суточный лимит озвучки → переход на запасную модель; ошибка картинки → шаблонная обложка.
- Публикация на моках: VK (ошибки 5, 502, 429, таймаут, капча, суточный лимит, токен группы → только текст с пометкой), Threads, Instagram, YouTube (протухший токен, квота, 503) — ошибка одной площадки не останавливает остальные, сообщения понятные.
- `/schedule`: 10:00 Europe/Moscow → 07:00 UTC; несуществующая дата и неполное время → подсказка формата.
- `.env.example` ↔ `config.py`: лишних переменных нет; все поля `Settings` используются в коде.
- README ↔ код: все модули `python -m …` существуют (включая `leadhunter.browser.login`), все команды бота из README есть в коде.
- `pyproject.toml`: все импортируемые пакеты перечислены; в wheel и в установленном пакете есть `miniapp/static/app.css`, `app.js`, `index.html`.
- `Dockerfile`: образ собирается за 1 мин 40 с (2,51 ГБ); внутри ffmpeg, DejaVu, cloudflared, Chromium, часовые пояса. С минимальным окружением (только фиктивный `BOT_TOKEN`, без сети) бот стартует, создаёт `data/leads.db` и останавливается на подключении к Telegram.
- `update.sh`: обновление с новым коммитом и `pip install` проходит (версия 0.1.0 → 0.1.1); локальные правки — корректное сообщение.

## Не удалось проверить

- **Настоящие площадки** (Telegram-канал, VK, Threads, Instagram, YouTube): нет тестовых токенов, рабочий бот трогать нельзя. Отсюда не подтверждены вживую №19 (PNG как `.jpg`), №20 (ответ Telegram на оборванный HTML) и гипотеза, что YouTube отклонит заголовок с `<`/`>` (мок показал, что в заголовок уходит `Заголовок <b> ролика`).
- **Настоящие LLM и Gemini** (тексты, картинки, озвучка, ИИ-видео `/aivideo`): нет ключей; проверено только на моках.
- **Docker-ветка `install.sh` до конца** (`docker compose up -d` с запущенным ботом) и **systemd-ветка** (`systemctl enable --now`): в контейнере нет демона Docker и systemd. Образ и разбор `.env` в Compose проверены отдельно.
- **macOS-ветка установщика** (`brew`) — нет машины.
- **Окончания строк в репозитории на GitHub** — репозиторий недоступен (404).
- **Установка браузера** (`patchright install chromium`) в Python-режиме — в автоматическом режиме вопрос по умолчанию «нет»; в Docker-образе Chromium ставится.
- **Отдельные агенты**: дочерние сессии из этой проверки не запускались — область проверена одной сессией.

## Приложения

- `sheet_carousel.png`, `sheet_reel.png`, `sheet_toon.png` — кадры карусели, ролика и мультика.
- `sheet_edge.png` — длинные слова и тексты; `post_1.png` — обложка поста; `carousel_1.png` — слайд в полном размере.
- `sheet_toon5.png` (№30), `sheet_toon_seed.png` (№29).
- `reel.mp4`, `toon.mp4` — примеры роликов.
- `checks.zip` — проверочные скрипты, их логи и логи установки.
