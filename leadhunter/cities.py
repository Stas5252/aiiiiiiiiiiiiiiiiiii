"""Города России для обхода карт: «Россия» в списке городов разворачивается в этот перечень."""

import hashlib

# Города с населением от ~250 тыс. человек, от крупных к меньшим
RU_CITIES = [
    "Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань", "Нижний Новгород", "Челябинск",
    "Красноярск", "Самара", "Уфа", "Ростов-на-Дону", "Краснодар", "Омск", "Воронеж", "Пермь", "Волгоград",
    "Саратов", "Тюмень", "Тольятти", "Махачкала", "Барнаул", "Ижевск", "Хабаровск", "Ульяновск", "Иркутск",
    "Владивосток", "Ярославль", "Севастополь", "Ставрополь", "Томск", "Кемерово", "Набережные Челны", "Оренбург",
    "Новокузнецк", "Балашиха", "Рязань", "Чебоксары", "Калининград", "Пенза", "Липецк", "Киров", "Астрахань",
    "Тула", "Сочи", "Курск", "Улан-Удэ", "Сургут", "Тверь", "Магнитогорск", "Брянск", "Якутск", "Иваново",
    "Владимир", "Симферополь", "Нижний Тагил", "Калуга", "Белгород", "Чита", "Грозный", "Волжский", "Смоленск",
    "Подольск", "Саранск", "Вологда", "Курган", "Череповец", "Архангельск", "Орёл", "Владикавказ",
    "Мурманск", "Тамбов", "Стерлитамак", "Петрозаводск", "Кострома", "Нижневартовск", "Новороссийск", "Йошкар-Ола",
    "Таганрог", "Химки", "Комсомольск-на-Амуре", "Сыктывкар", "Нальчик", "Нижнекамск", "Шахты", "Дзержинск",
    "Братск", "Орск", "Энгельс", "Ангарск", "Благовещенск", "Великий Новгород", "Старый Оскол", "Псков",
]
# Кому чаще всего нужен сайт или его переделка: местные услуги, где клиента выбирают по сайту и записи
SITE_RUBRICS = [
    "банкетный зал", "база отдыха", "стоматология", "косметология", "салон красоты", "стилист", "фотостудия",
    "компьютерный клуб", "фитнес клуб", "школа танцев", "детский центр", "психолог", "юридические услуги",
    "автосервис", "ремонт квартир", "свадебное агентство",
]
ALL_RUSSIA = {"россия", "вся россия", "рф", "вся рф", "russia"}


def expand_cities(cities: list[str]) -> list[str]:
    """«Россия» (или «РФ») в списке заменяется на города России; остальные города остаются, повторы убираются."""
    out: list[str] = []
    for c in cities:
        c = c.strip()
        if not c:
            continue
        out += RU_CITIES if c.lower().replace("ё", "е") in ALL_RUSSIA else [c]
    return list(dict.fromkeys(out))


def split_list(value: str) -> list[str]:
    return [x.strip() for x in (value or "").replace(";", ",").replace("\n", ",").split(",") if x.strip()]


def pair_at(cities: list[str], queries: list[str], k: int) -> tuple[str, str]:
    """k-я пара обхода: каждый шаг — следующий город и следующая рубрика; за len(cities) * len(queries) шагов
    каждая рубрика пройдена в каждом городе ровно один раз."""
    c = k % len(cities)
    return cities[c], queries[(c + k // len(cities)) % len(queries)]


class MapsPlan:
    """Что искать на картах в этом проходе. Города и рубрики — из кабинета (если заданы) или из .env; за проход
    берётся не больше per_poll пар «рубрика + город», место обхода хранится в базе и переживает перезапуск."""

    CURSOR = "maps_cursor"

    def __init__(self, cities: list[str], queries: list[str], per_poll: int, db=None):
        self.cities = cities
        self.queries = queries
        self.per_poll = per_poll
        self.db = db

    async def lists(self) -> tuple[list[str], list[str]]:
        cities, queries = self.cities, self.queries
        if self.db:
            cities = split_list(await self.db.get_setting("maps_cities", "")) or cities
            queries = split_list(await self.db.get_setting("maps_queries", "")) or queries
        return expand_cities(cities), list(dict.fromkeys(queries))

    @staticmethod
    def stamp(cities: list[str], queries: list[str]) -> str:
        return hashlib.sha1("\n".join([*cities, "#", *queries]).encode()).hexdigest()[:12]

    async def cursor(self, cities: list[str], queries: list[str]) -> int:
        """Место обхода. Оно относится к конкретным спискам: сменили города или рубрики — обход идёт с начала нового
        списка, а не с середины (уже пройденные пары повторятся, но их лиды в базе есть и второй раз не придут)."""
        pos, _, stamp = (await self.db.get_setting(self.CURSOR, "") if self.db else "").partition(":")
        return int(pos) if pos.isdigit() and stamp == self.stamp(cities, queries) else 0

    async def batch(self) -> tuple[list[tuple[str, str]], str]:
        """Пары (город, рубрика) на этот проход и место, с которого начнётся следующий (его передают в commit)."""
        cities, queries = await self.lists()
        total = len(cities) * len(queries)
        if not total:
            return [], ""
        start = await self.cursor(cities, queries) % total
        count = min(max(self.per_poll, 1), total)
        return ([pair_at(cities, queries, (start + i) % total) for i in range(count)],
                f"{(start + count) % total}:{self.stamp(cities, queries)}")

    async def commit(self, cursor: str) -> None:
        if self.db and cursor:
            await self.db.set_setting(self.CURSOR, cursor)

    async def describe(self) -> str:
        """Одна строка для бота и кабинета: сколько городов и рубрик и как идёт обход."""
        p = await self.progress()
        if not p["total"]:
            return "Города или рубрики не заданы — карты не опрашиваются."
        if p["total"] <= p["per_poll"]:
            return f"Городов: {p['cities']}, рубрик: {p['queries']}. Все {p['total']} запросов — за один проход."
        return (f"Городов: {p['cities']}, рубрик: {p['queries']} — всего {p['total']} запросов «рубрика + город». "
                f"За проход бот делает {p['per_poll']}, пройдено {p['done']} из {p['total']}.")

    async def progress(self) -> dict:
        cities, queries = await self.lists()
        total = len(cities) * len(queries)
        return {"cities": len(cities), "queries": len(queries), "total": total,
                "done": await self.cursor(cities, queries) % total if total else 0, "per_poll": min(max(self.per_poll, 1), total or 1)}
