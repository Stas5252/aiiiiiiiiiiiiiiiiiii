"""Технический разбор сайта по главной странице, robots.txt и карте сайта. Только то, что видно без входа."""
import html
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from ..audit import PARKED_RE, VIEWPORT_RE
from ..extract import EMAIL_RE, PHONE_RE
from .fetch import Page, fetch, normalize_url

AGENT = "LeadHunterBot"
SLOW_SECONDS = 3.0
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC = re.compile(r"<meta\b(?=[^>]*\bname=[\"']description[\"'])[^>]*\bcontent=[\"']([^\"']*)[\"']", re.I)
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_FORM = re.compile(r"<form\b", re.I)
_SCHEMA = re.compile(r"application/ld\+json|itemtype=[\"']https?://schema\.org", re.I)
_GENERATOR = re.compile(r"<meta\b(?=[^>]*\bname=[\"']generator[\"'])[^>]*\bcontent=[\"']([^\"']*)[\"']", re.I)
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


def detect_cms(body: str) -> str:
    for name, pattern in _CMS:
        if re.search(pattern, body, re.I):
            return name
    m = _GENERATOR.search(body)
    return _clean(m.group(1), 40) if m else ""


def parse_page(page: Page, *, now: datetime | None = None) -> dict:
    """Факты о главной странице и список проблем простыми словами."""
    body = page.body
    title = _clean(m.group(1)) if (m := _TITLE.search(body)) else ""
    desc = _clean(m.group(1), 300) if (m := _META_DESC.search(body)) else ""
    h1 = [_clean(x) for x in _H1.findall(body)]
    h1 = [x for x in h1 if x]
    year = footer_year(body)
    phones = list(dict.fromkeys(re.sub(r"\s+", " ", p.strip()) for p in PHONE_RE.findall(_TAG.sub(" ", body))))[:3]
    report = {
        "https": page.url.startswith("https://"), "mobile": bool(VIEWPORT_RE.search(body)),
        "seconds": page.seconds, "kb": round(len(body.encode()) / 1024),
        "title": title, "description": desc, "h1": h1[0] if h1 else "", "h1_count": len(h1),
        "schema": bool(_SCHEMA.search(body)), "forms": len(_FORM.findall(body)),
        "phones": phones, "emails": list(dict.fromkeys(EMAIL_RE.findall(_TAG.sub(" ", body))))[:2],
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


def robots_allows(robots: str, url: str) -> bool:
    parser = RobotFileParser()
    parser.parse(robots.splitlines())
    return parser.can_fetch(AGENT, url)


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
    sitemap = bool(has_robots and re.search(r"^\s*sitemap\s*:", robots.body, re.I | re.M))
    if not sitemap:
        sm = await get(urljoin(root, "sitemap.xml"))
        sitemap = sm.ok and sm.status == 200 and bool(re.search(r"<(?:urlset|sitemapindex)\b", sm.body[:5000], re.I))
    report["sitemap"] = sitemap
    if not has_robots:
        report["problems"].append("нет robots.txt")
    if not sitemap:
        report["problems"].append("нет карты сайта (sitemap.xml)")
    return report
