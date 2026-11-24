"""Сборка досье на лид. Что входит — зависит от лида:
компания с карт и сайт есть — сайт, место в поиске, конкуренты, гео; сайта нет — конкуренты и гео;
заказ с биржи или из чата — сайт по ссылке из текста, если она есть. В конце — вывод ИИ и первое сообщение."""
import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone

from .. import brief
from ..http import CaptchaError, Fetcher
from ..sources.yandex_maps_web import URL as MAPS_URL, clean_site, parse_yandex_items
from .fetch import fetch
from .search import position, search_urls
from .site import analyze_site, domain_of

log = logging.getLogger(__name__)

COMPANY_SOURCES = ("yandex_maps", "site_audit")
SITE_CACHE_DAYS = 7
MAX_COMPETITORS = 4
AUTO_PER_HOUR = 6
AI_TIMEOUT = 60  # секунд ждём вывод ИИ: перегруженная модель иначе держит досье минутами
# Ссылки из текста заказа, которые точно не сайт заказчика: документы с ТЗ, макеты, соцсети, сами биржи
NOT_CLIENT_SITE = (
    "google.com", "yandex.ru", "ya.ru", "yadi.sk", "figma.com", "t.me", "telegram.me", "telegram.org", "vk.com",
    "vk.me", "instagram.com", "threads.net", "youtube.com", "youtu.be", "github.com", "notion.so", "notion.site",
    "dropbox.com", "miro.com", "behance.net", "pinterest.com", "pinterest.ru", "canva.com", "kwork.ru", "fl.ru",
    "freelance.ru", "weblancer.net", "hh.ru", "wa.me", "whatsapp.com", "mail.ru", "ok.ru", "dzen.ru", "tilda.cc",
    "wordpress.org", "wordpress.com", "apple.com", "rutube.ru", "avito.ru", "habr.com", "trello.com", "loom.com",
)
_URL = re.compile(r"https?://[^\s<>\"')\]]+|(?<![\w@./-])(?:[a-zа-яё0-9][a-zа-яё0-9-]*\.)+(?:ru|рф|su|by|kz)(?![\w-])",
                  re.I)

SYSTEM = """Ты — помощник исполнителя (фрилансера или студии) по продажам. Бриф исполнителя:
{profile}

Тебе дают лид и досье на него — факты, которые собрала программа: разбор сайта, место в поиске, конкуренты, гео.
Сделай вывод и напиши первое сообщение клиенту.
Правила:
- Опирайся только на досье и бриф. Проблем, цифр, кейсов и контактов, которых там нет, не выдумывай.
- Цену называй только если она есть в брифе на эту услугу; нет — сумм не называй, поле price оставь пустым.
- Если сайт помечен «ссылка из текста заказа» — это может быть пример, а не сайт заказчика: не утверждай, что это его сайт.
- Программа видит только код главной страницы. «forms: 0» значит, что в коде главной формы не нашлось, а не что её нет
  на сайте: пиши осторожно — «на главной не увидел формы заявки».
- Не поучай и не ругай сайт клиента: назови 2–3 конкретных факта из досье и что это ему даёт, если исправить.
Ответь строго JSON без пояснений:
{{"pain": "что у клиента болит — 1–2 предложения по фактам досье; фактов нет — что нужно выяснить",
 "offer": "что предложить из услуг брифа — одна строка",
 "price": "цена из брифа на эту услугу или пустая строка",
 "questions": ["до трёх вопросов, которые стоит задать клиенту"],
 "message": "первое сообщение клиенту от лица исполнителя: 3–5 предложений в тоне из брифа, 2–3 факта из досье (про его сайт, поиск или конкурентов), один вопрос в конце и контакт из брифа; контакта в брифе нет — никакого не называй. О себе — как в брифе; если не ясно, мужчина это, женщина или команда, — без рода"}}"""


class DossierError(Exception):
    """Досье собрать нельзя — текст можно показать человеку."""


def site_from_text(text: str) -> str:
    """Первая ссылка из текста заказа, похожая на сайт заказчика."""
    for m in _URL.finditer(text or ""):
        url = m.group().rstrip(".,;:!?")
        host = domain_of(url)
        if host and "." in host and not any(host == bad or host.endswith("." + bad) for bad in NOT_CLIENT_SITE):
            return url
    return ""


def subject(row) -> dict:
    """О ком досье: вид лида, сайт, рубрика, город и прочее, что знает источник."""
    keys = row.keys()
    try:
        meta = json.loads(row["meta"]) if "meta" in keys and row["meta"] else {}
    except (TypeError, ValueError):
        meta = {}
    source, text = row["source"], row["text"] or ""
    company = source in COMPANY_SOURCES
    if company and not meta.get("city"):
        # Лиды, сохранённые до появления meta: «Рубрики, Город. Адрес. …» и «Сайт example.ru: проблема»
        head = text.split(". ")[0]
        cats, _, city = head.rpartition(", ")
        meta = {"category": cats.split(",")[0].strip(), "city": city.strip(), **meta}
        if m := re.search(r"Сайт (\S+?): ", text):  # двоеточие с пробелом: в «http://…» оно не конец адреса
            meta.setdefault("site", m.group(1))
    site = meta.get("site") or ("" if company else site_from_text(f"{row['title'] or ''}\n{text}"))
    return {"kind": ("company_site" if site else "company_nosite") if company else "order",
            "site": site, "site_from_text": bool(site) and not company,
            "category": meta.get("category", ""), "city": meta.get("city", ""), "address": meta.get("address", ""),
            "rating": meta.get("rating"), "reviews": meta.get("reviews"),
            "org_id": str(row["external_id"]).removeprefix("audit:") if company else "", "title": row["title"] or ""}


def has_subject(row) -> bool:
    """Есть ли что разбирать без ИИ: компания с карт или ссылка на сайт в тексте заказа."""
    return bool(row["source"] in COMPANY_SOURCES or subject(row)["site"])


def competitor_rows(items: list[dict], *, org_id: str, domain: str, limit: int = MAX_COMPETITORS) -> tuple[dict, list[dict]]:
    """Из выдачи карт: сколько компаний рубрики имеют сайт и первые limit конкурентов с сайтом (не сам клиент)."""
    others = [x for x in items if str(x.get("id")) != org_id]
    rows = []
    for x in others:
        if not x.get("urls"):
            continue
        site = clean_site(x["urls"][0])
        if domain_of(site) == domain or any(domain_of(site) == domain_of(r["site"]) for r in rows):
            continue
        rating = x.get("ratingData") or {}
        rows.append({"title": x.get("title", ""), "site": site, "rating": rating.get("ratingValue"),
                     "reviews": rating.get("reviewCount")})
        if len(rows) >= limit:
            break
    return {"total": len(others), "with_site": sum(1 for x in others if x.get("urls"))}, rows


def _brief_report(report: dict) -> dict:
    """Короткая версия разбора — для конкурентов и для ИИ."""
    keys = ("domain", "ok", "error", "https", "mobile", "seconds", "title", "description", "h1", "cms", "year",
            "robots", "sitemap", "schema", "forms", "problems")
    return {k: report[k] for k in keys if k in report}


class DossierService:
    def __init__(self, db, get_llm, *, proxy: str | None = None, get_serper_key=lambda: None, default_profile: str = "",
                 get=fetch, search=search_urls, maps: Fetcher | None = None):
        self.db = db
        # ИИ и ключ поиска меняются без перезапуска (/setup) — берём текущие в момент сборки
        self.get_llm = get_llm
        self.get_serper_key = get_serper_key
        self.default_profile = default_profile
        self.get = get
        self.search = search
        self.maps = maps or Fetcher(proxy, pause=(12, 30))
        self._maps_lock = asyncio.Lock()
        self._sites = asyncio.Semaphore(3)
        self._tasks: dict[int, asyncio.Task] = {}
        self.errors: dict[int, str] = {}
        self._auto: deque[float] = deque()

    # --- запуск в фоне ---
    def running(self, lead_id: int) -> bool:
        task = self._tasks.get(lead_id)
        return bool(task and not task.done())

    def start(self, lead_id: int, *, force: bool = False, on_done=None) -> bool:
        """Собирает досье в фоне; on_done(досье | None, ошибка) вызывается в конце. False — уже собирается."""
        if self.running(lead_id):
            return False
        self.errors.pop(lead_id, None)

        async def job():
            data, error = None, ""
            try:
                data = await self.build(lead_id, force=force)
            except DossierError as e:
                error = str(e)
            except Exception as e:
                log.exception("досье на лид %s не собралось", lead_id)
                error = f"Досье не собралось ({type(e).__name__}). Подробности — в логе бота."
            if error:
                self.errors[lead_id] = error
            if on_done:
                try:
                    await on_done(data, error)
                except Exception:
                    log.exception("не удалось отправить досье на лид %s", lead_id)

        task = asyncio.create_task(job(), name=f"dossier-{lead_id}")
        self._tasks[lead_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(lead_id, None) if self._tasks.get(lead_id) is t else None)
        return True

    def auto_allowed(self) -> bool:
        """Автоматических досье — не больше AUTO_PER_HOUR в час: каждое — запросы к чужим сайтам, картам и ИИ."""
        now = time.monotonic()
        while self._auto and now - self._auto[0] > 3600:
            self._auto.popleft()
        if len(self._auto) >= AUTO_PER_HOUR:
            return False
        self._auto.append(now)
        return True

    # --- части досье ---
    async def site_report(self, url: str, *, force: bool = False) -> dict:
        domain = domain_of(url)
        if not force and (cached := await self.db.get_site_report(domain, SITE_CACHE_DAYS)):
            return cached
        async with self._sites:
            report = await analyze_site(url, get=self.get)
        await self.db.save_site_report(domain, report)
        return report

    async def _competitors(self, sub: dict, notes: list[str]) -> tuple[dict | None, list[dict]]:
        query = f"{sub['category']} {sub['city']}".strip()
        try:
            async with self._maps_lock:
                r = await self.maps.get(MAPS_URL, params={"text": query})
            r.raise_for_status()
            items = parse_yandex_items(r.text)
        except CaptchaError:
            notes.append("Карты показали капчу — конкурентов собрать не удалось, попробуйте позже.")
            return None, []
        except Exception as e:
            log.warning("досье: карты не ответили на «%s»: %s", query, type(e).__name__)
            notes.append("Карты не ответили — конкурентов собрать не удалось.")
            return None, []
        market, rows = competitor_rows(items, org_id=sub["org_id"], domain=domain_of(sub["site"]) if sub["site"] else "")
        reports = await asyncio.gather(*(self.site_report(r["site"]) for r in rows), return_exceptions=True)
        for row, report in zip(rows, reports):
            row["report"] = _brief_report(report) if isinstance(report, dict) else {"ok": False, "error": "не удалось проверить"}
        return {"query": query, **market}, rows

    async def _seo(self, sub: dict, notes: list[str]) -> dict | None:
        query = f"{sub['category']} {sub['city']}".strip()
        try:
            engine, urls = await self.search(query, self.get_serper_key())
        except Exception as e:
            log.warning("досье: поиск не ответил на «%s»: %s", query, type(e).__name__)
            notes.append("Поисковик не ответил — место сайта в поиске не проверено.")
            return None
        if not urls:
            notes.append("Поиск ничего не вернул — место сайта в поиске не проверено."
                         + ("" if self.get_serper_key() else " С ключом Serper (поиск Google) проверка надёжнее: /setup"))
            return None
        # depth — сколько результатов поисковик реально отдал: «в первых 7 не найден» честнее, чем «в первых 20»
        return {"query": query, "engine": engine, "position": position(domain_of(sub["site"]), urls), "depth": len(urls)}

    async def _conclude(self, row, data: dict, notes: list[str]) -> dict | None:
        llm = self.get_llm()
        if not llm:
            notes.append("ИИ не подключён — это только технический разбор, без вывода и первого сообщения.")
            return None
        facts = {"вид лида": {"company_site": "компания с карт, сайт есть", "company_nosite": "компания с карт, сайта нет",
                              "order": "заказ с биржи или из чата"}[data["kind"]],
                 "сайт": ({**_brief_report(data["site"]),
                           "откуда": "ссылка из текста заказа" if data["site_from_text"] else "карточка компании на картах"}
                          if data["site"] else "нет"),
                 "место в поиске": data["seo"] or "не проверялось",
                 "рынок": data["market"] or "не проверялся", "конкуренты": data["competitors"], "гео": data["geo"] or "нет"}
        user = (f"Лид ({row['source']}): {row['title'] or ''}\n{(row['text'] or '')[:3000]}\n\n"
                f"Досье:\n{json.dumps(facts, ensure_ascii=False)}")
        profile = await brief.profile(self.db, self.default_profile, with_min=False)
        try:
            out = await asyncio.wait_for(llm.complete_json(SYSTEM.format(profile=profile), user, temperature=0.3),
                                         AI_TIMEOUT)
        except Exception as e:
            log.warning("досье: ИИ не ответил: %s", type(e).__name__)
            notes.append("ИИ не ответил — это только технический разбор. Нажмите «Обновить» позже.")
            return None
        questions = out.get("questions") if isinstance(out.get("questions"), list) else []
        return {"pain": str(out.get("pain") or "")[:600], "offer": str(out.get("offer") or "")[:300],
                "price": str(out.get("price") or "")[:120], "questions": [str(q)[:200] for q in questions[:3] if q],
                "message": str(out.get("message") or "")[:1500]}

    # --- сборка ---
    async def build(self, lead_id: int, *, force: bool = False) -> dict:
        """Готовое досье берётся из базы; force — собрать заново (разбор сайта тоже без кэша)."""
        if not force and (stored := await self.db.get_dossier(lead_id)):
            return stored
        row = await self.db.get_lead(lead_id)
        if not row:
            raise DossierError("Лид не найден")
        sub = subject(row)
        if sub["kind"] == "order" and not sub["site"] and not self.get_llm():
            raise DossierError("В этом заказе нет ссылки на сайт, а ИИ не подключён — досье собрать не из чего. "
                               "Подключите ИИ: /setup")
        notes: list[str] = []
        data: dict = {"version": 1, "kind": sub["kind"], "title": sub["title"], "site": None,
                      "site_from_text": sub["site_from_text"], "seo": None, "market": None, "competitors": [], "geo": None}
        company = sub["kind"] != "order"
        can_compare = company and sub["category"] and sub["city"]
        jobs = {}
        if sub["site"]:
            jobs["site"] = self.site_report(sub["site"], force=force)
            if can_compare:
                jobs["seo"] = self._seo(sub, notes)
        if can_compare:
            jobs["competitors"] = self._competitors(sub, notes)
        elif company:
            notes.append("Рубрика или город компании неизвестны — конкуренты и место в поиске не проверялись.")
        done = dict(zip(jobs, await asyncio.gather(*jobs.values())))
        data["site"] = done.get("site")
        data["seo"] = done.get("seo")
        if "competitors" in done:
            data["market"], data["competitors"] = done["competitors"]
        if company:
            data["geo"] = {k: sub[k] for k in ("city", "address", "category", "rating", "reviews") if sub.get(k)}
        if sub["site_from_text"]:
            notes.append("Сайт взят из ссылки в тексте заказа — это может быть пример, а не сайт заказчика.")
        if sub["kind"] == "order" and not sub["site"]:
            notes.append("В заказе нет ссылки на сайт — разбирать нечего, ниже только вывод ИИ.")
        data["ai"] = await self._conclude(row, data, notes)
        data["notes"] = notes
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        await self.db.save_dossier(lead_id, data)
        return data
