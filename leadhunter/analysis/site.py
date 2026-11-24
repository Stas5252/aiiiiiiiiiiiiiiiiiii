"""Технический разбор сайта по главной странице, robots.txt и карте сайта. Только то, что видно без входа."""
import html
import re
from itertools import islice
from datetime import datetime
from urllib.parse import quote, urljoin, urlsplit

from ..audit import PARKED_RE
from ..extract import EMAIL_RE, PHONE_RE
from .fetch import Page, fetch, normalize_url

AGENT = "LeadHunterBot"
SLOW_SECONDS = 3.0
# Страницу пишет владелец чужого сайта, а ссылку на неё — автор заказа. Шаблоны ниже устроены так, чтобы время
# разбора росло вместе с размером страницы, а не с его квадратом: в «[^<>]*» нет «<», поэтому на мусоре вроде
# «<meta <meta <meta …» каждая попытка обрывается на следующем же «<»; конец тега ищется в ограниченном окне.
_TAG = re.compile(r"<[^<>]*>")
_OPEN = {name: re.compile(rf"<{name}\b[^<>]*>", re.I) for name in ("title", "h1", "meta")}
_CLOSE = {name: re.compile(rf"</{name}\b", re.I) for name in ("title", "h1")}
_ATTR_NAME = re.compile(r"\bname\s*=\s*[\"']?([\w:.-]+)", re.I)
_ATTR_CONTENT = re.compile(r"\bcontent\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I)
INNER_LIMIT = 3000  # символов содержимого <title> и <h1> — больше в заголовке не бывает
META_LIMIT = 300  # тегов <meta> на странице читаем не больше
_FORM = re.compile(r"<form\b", re.I)
_SCHEMA = re.compile(r"application/ld\+json|itemtype=[\"']https?://schema\.org", re.I)
_COPY_YEAR = re.compile(r"(?:©|&copy;|\(c\)|copyright)[^<]{0,80}", re.I)
_YEAR = re.compile(r"(?:19|20)\d\d")
_MESSENGERS = (("WhatsApp", r"wa\.me/|api\.whatsapp\.com|whatsapp://"), ("Telegram", r"(?:t\.me|telegram\.me)/|tg://"),
               ("VK", r"vk\.com/|vk\.me/"), ("Viber", r"viber://"))
_CMS = (("Tilda", r"tildacdn\.|tilda\.ws|static\.tildacdn"), ("WordPress", r"/wp-content/|/wp-includes/"),
        ("1С-Битрикс", r"/bitrix/(?:js|templates|cache)"), ("Wix", r"wixstatic\.com|wix\.com"),
        ("Joomla", r"/media/jui/|joomla"), ("MODX", r"/assets/components/|modx"), ("OpenCart", r"catalog/view/theme"),
        ("InSales", r"insales\.ru|static\.insales-cdn"), ("Nethouse", r"nethouse\.ru"), ("uCoz", r"ucoz\.(?:ru|net)"),
        ("Shopify", r"cdn\.shopify\.com"), ("Flexbe", r"flexbe"), ("Taplink", r"taplink\.(?:cc|ws)"),
        ("Webflow", r"webflow\.(?:com|io)"), ("Craftum", r"craftum"), ("LPgenerator", r"lpgenerator"))


def domain_of(url: str) -> str:
    host = (urlsplit(normalize_url(url)).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _clean(fragment: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()[:limit]


def footer_year(body: str) -> int | None:
    """Самый поздний год рядом со знаком ©: «© 2015–2019» → 2019."""
    years = [int(y) for m in _COPY_YEAR.finditer(body) for y in _YEAR.findall(m.group())]
    return max(years) if years else None


def inner(body: str, tag: str, limit: int = 3) -> list[str]:
    """Содержимое первых limit тегов <tag>…</tag>."""
    out = []
    for m in islice(_OPEN[tag].finditer(body), limit):
        close = _CLOSE[tag].search(body, m.end(), m.end() + INNER_LIMIT)
        out.append(body[m.end():close.start()] if close else "")
    return out


def metas(body: str) -> dict[str, str]:
    """<meta name=… content=…> страницы: имя в нижнем регистре → значение (первое с таким именем)."""
    found: dict[str, str] = {}
    for m in islice(_OPEN["meta"].finditer(body), META_LIMIT):
        name, content = _ATTR_NAME.search(m.group()), _ATTR_CONTENT.search(m.group())
        if name and content:
            found.setdefault(name.group(1).lower(), content.group(1) if content.group(1) is not None else content.group(2))
    return found


def detect_cms(body: str) -> str:
    for name, pattern in _CMS:
        if re.search(pattern, body, re.I):
            return name
    return _clean(metas(body).get("generator", ""), 40)


def parse_page(page: Page, *, now: datetime | None = None) -> dict:
    """Факты о главной странице и список проблем простыми словами."""
    body = page.body
    meta = metas(body)
    title = next((_clean(x) for x in inner(body, "title", 1)), "")
    desc = _clean(meta.get("description", ""), 300)
    h1 = [x for x in (_clean(x) for x in inner(body, "h1")) if x]
    year = footer_year(body)
    text = re.sub(r"\s+", " ", _TAG.sub(" ", body))
    phones = list(dict.fromkeys(p.strip() for p in PHONE_RE.findall(text)))[:3]
    # почту ищем по словам с «@»: шаблон почты на длинной строке без пробелов считался бы слишком долго
    emails = [m.group() for word in text.split() if "@" in word and len(word) <= 100 and (m := EMAIL_RE.search(word))]
    report = {
        "https": page.url.startswith("https://"), "mobile": "viewport" in meta,
        "seconds": page.seconds, "kb": round(len(body.encode()) / 1024),
        "title": title, "description": desc, "h1": h1[0] if h1 else "",
        "h1_count": sum(1 for _ in _OPEN["h1"].finditer(body)),
        "schema": bool(_SCHEMA.search(body)), "forms": len(_FORM.findall(body)),
        "phones": phones, "emails": list(dict.fromkeys(emails))[:2],
        "messengers": [name for name, pattern in _MESSENGERS if re.search(pattern, body, re.I)],
        "cms": detect_cms(body), "year": year,
    }
    problems = []
    if not report["https"]:
        problems.append("без HTTPS")
    if not report["mobile"]:
        problems.append("не адаптирован под телефон")
    if PARKED_RE.search(body):
        problems.append("заглушка вместо сайта")
    if len(body) < 1500:
        problems.append("почти пустая страница")
    if page.seconds >= SLOW_SECONDS:
        problems.append(f"отвечает медленно ({page.seconds:.1f} с)")
    if not title:
        problems.append("нет заголовка страницы (title)")
    if not desc:
        problems.append("нет описания для поиска (description)")
    if not h1:
        problems.append("нет главного заголовка (h1)")
    if not report["forms"] and not phones and not report["messengers"]:
        problems.append("на главной нет ни формы заявки, ни телефона, ни мессенджера")
    if year and year < (now or datetime.now()).year - 1:
        problems.append(f"в подвале {year} год — сайт давно не обновляли")
    report["problems"] = problems
    return report


def robots_rules(robots: str, agent: str = AGENT) -> list[tuple[bool, str]]:
    """Правила (разрешено?, шаблон пути) для нашего робота: его собственная группа, иначе группа «*»."""
    groups: dict[str, list[tuple[bool, str]]] = {}
    current: list[str] = []
    in_rules = False
    for line in robots.splitlines():
        key, sep, value = line.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if not sep:
            continue
        if key == "user-agent":
            if in_rules:  # после правил начинается новая группа
                current, in_rules = [], False
            current.append(value.lower())
            groups.setdefault(value.lower(), [])
        elif key in ("allow", "disallow") and current:
            in_rules = True
            if value:  # пустой Disallow ничего не запрещает
                for name in current:
                    groups[name].append((key == "allow", value))
    own = next((rules for name, rules in groups.items() if name != "*" and name in agent.lower()), None)
    return own if own is not None else groups.get("*", [])


def _rule_matches(pattern: str, path: str) -> bool:
    """Шаблон robots.txt: «*» — любые символы, «$» в конце — конец адреса; без «$» — начало пути.
    Без регулярных выражений: шаблон пишет владелец чужого сайта, и «/*a*a*a*a$» не должен подвешивать бота —
    куски между «*» ищутся по очереди слева направо, это один проход по адресу."""
    anchored = pattern.endswith("$")
    first, *rest = (pattern[:-1] if anchored else pattern).split("*")
    if not path.startswith(first):
        return False
    if not rest:
        return not anchored or len(path) == len(first)
    pos = len(first)
    last = rest.pop()
    for part in rest:
        found = path.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    if anchored:
        return len(path) - len(last) >= pos and path.endswith(last)
    return path.find(last, pos) >= 0


_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")


def _canon(value: str) -> str:
    """Одна запись для пути и для правила: «/каталог/» и «/%d0%ba…» — один путь, а «%2F» и «/» — разные.
    Кириллица кодируется, «%41» раскрывается в «A», остальные %-коды остаются как есть, заглавными."""
    def fix(m: re.Match) -> str:
        char = chr(int(m.group()[1:], 16))
        return char if char in _UNRESERVED else m.group().upper()

    return _ESCAPE.sub(fix, quote(value, safe="/?#[]@!$&'()*+,;=:%-._~"))


def robots_allows(robots: str, url: str) -> bool:
    """Как у поисковиков: побеждает самое длинное подходящее правило, при равной длине — разрешающее.
    Стандартный разборщик Python не понимает «*» и «$» и берёт первое подходящее правило, а не самое точное."""
    parts = urlsplit(url)
    path = _canon((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
    best_len, allowed = -1, True
    for allow, pattern in robots_rules(robots):
        pattern = _canon(pattern)
        if _rule_matches(pattern, path) and (len(pattern) > best_len or (len(pattern) == best_len and allow)):
            best_len, allowed = len(pattern), allow
    return allowed


async def analyze_site(url: str, *, get=fetch, now: datetime | None = None) -> dict:
    """До трёх запросов: robots.txt, главная, sitemap.xml. Сайт, закрытый в robots.txt, не читаем —
    и это само по себе находка: поисковики его тоже не видят."""
    url = normalize_url(url)
    root = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"
    report: dict = {"url": url, "domain": domain_of(url), "ok": False, "error": "", "problems": []}
    robots = await get(urljoin(root, "robots.txt"))
    has_robots = robots.ok and robots.status == 200 and "<html" not in robots.body[:500].lower()
    report["robots"] = has_robots
    if has_robots and not robots_allows(robots.body, url):
        report.update(error="закрыт от роботов в robots.txt", problems=["закрыт от поисковиков в robots.txt"])
        return report
    page = await get(url)
    if not page.ok:
        report["error"] = page.error or f"отдаёт ошибку {page.status}"
        report["problems"] = [report["error"] if report["error"].startswith("сайт") else "сайт " + report["error"]]
        return report
    report.update(parse_page(page, now=now), ok=True, url=page.url)
    sitemap = has_robots and any(line.partition(":")[0].strip().lower() == "sitemap" for line in robots.body.splitlines())
    if not sitemap:
        sm = await get(urljoin(root, "sitemap.xml"))
        sitemap = sm.ok and sm.status == 200 and bool(re.search(r"<(?:urlset|sitemapindex)\b", sm.body[:5000], re.I))
    report["sitemap"] = sitemap
    if not has_robots:
        report["problems"].append("нет robots.txt")
    if not sitemap:
        report["problems"].append("нет карты сайта (sitemap.xml)")
    return report
