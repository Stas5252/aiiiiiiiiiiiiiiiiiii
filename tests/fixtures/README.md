# Фикстуры источников

Страницы сняты одиночными живыми запросами 22.11.2026 (без авторизации) и сжаты: убрана разметка
и поля, которые разбор не читает. Структура данных — как на площадке.

| Файл | Откуда |
|---|---|
| `kwork_projects.html` | `kwork.ru/projects?c=11` — `window.stateData.wantsListData` |
| `weblancer_jobs.html` | `weblancer.net/jobs/` — RSC-чанк `self.__next_f.push` со списком заказов |
| `fl_ru.xml` | `fl.ru/rss/all.xml` целиком |
| `freelance_ru_tasks.html` | `freelance.ru/task?q=сайт` — все карточки `<article class="task-card…">` |
| `yandex_maps_search.html` | `yandex.ru/maps/?text=ремонт обуви Тула` — выдача организаций |
| `threads_search.html` | `threads.net/search?q=нужен сайт&serp_type=recent` — Relay-кэш с постами |

Даты в фикстурах настоящие, поэтому тесты, зависящие от «свежести», должны подставлять своё «сейчас».
