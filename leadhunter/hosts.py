"""Чужие площадки: ссылка на них — не собственный сайт компании (соцсеть, мессенджер, справочник, агрегатор)."""
from urllib.parse import urlsplit

SOCIAL = (
    "vk.com", "vk.ru", "vk.me", "vk.link", "ok.ru", "t.me", "telegram.me", "telegram.org", "instagram.com",
    "threads.net", "facebook.com", "fb.com", "youtube.com", "youtu.be", "rutube.ru", "dzen.ru", "tiktok.com",
    "wa.me", "whatsapp.com", "viber.com", "max.ru", "pinterest.com", "pinterest.ru", "twitter.com", "x.com",
)
# Справочники организаций, отзовики, агрегаторы услуг, доски объявлений, маркетплейсы, сервисы записи и ссылок
DIRECTORIES = (
    "spravker.ru", "2gis.ru", "2gis.com", "zoon.ru", "yell.ru", "flamp.ru", "orgpage.ru", "spr.ru", "cataloxy.ru",
    "yp.ru", "rubrikator.org", "list-org.com", "rusprofile.ru", "sbis.ru", "checko.ru", "big-book-city.ru",
    "jsprav.ru", "ruscatalog.org", "infoorel.ru", "blizko.ru", "pulscen.ru", "tiu.ru", "satom.ru", "firmika.ru",
    "otzovik.com", "irecommend.ru", "tripadvisor.ru", "tripadvisor.com", "restoclub.ru", "afisha.ru", "kudago.com",
    "gorko.ru", "banket.ru", "prodoctorov.ru", "napopravku.ru", "docdoc.ru", "sberzdorovye.ru", "krasotaimedicina.ru",
    "profi.ru", "uslugi.yandex.ru", "youdo.com", "avito.ru", "youla.ru", "drom.ru", "auto.ru", "cian.ru",
    "domclick.ru", "ozon.ru", "wildberries.ru", "market.yandex.ru", "booking.com", "ostrovok.ru", "101hotels.com",
    "sutochno.ru", "yclients.com", "dikidi.net", "dikidi.ru", "taplink.cc", "taplink.ws", "linktr.ee",
    "mssg.me", "hipolink.me",
)
OTHER = (
    "google.com", "yandex.ru", "ya.ru", "yadi.sk", "mail.ru", "figma.com", "github.com", "notion.so", "notion.site",
    "dropbox.com", "miro.com", "behance.net", "canva.com", "kwork.ru", "fl.ru", "freelance.ru", "weblancer.net",
    "hh.ru", "tilda.cc", "wordpress.org", "wordpress.com", "apple.com", "habr.com", "trello.com", "loom.com",
)
FOREIGN_HOSTS = frozenset(SOCIAL + DIRECTORIES + OTHER)
# У «Яндекс Бизнеса» страница компании живёт на поддомене clients.site — это её собственная страница
OWN_ON_SUBDOMAIN = ("clients.site",)


def host_of(url: str) -> str:
    url = (url or "").strip()
    host = (urlsplit(url if "//" in url else "//" + url).hostname or "").lower().rstrip(".")
    return host.removeprefix("www.")


def foreign_host(url: str) -> str:
    """Чужая площадка, на которую ведёт ссылка («spravker.ru» для saratov.spravker.ru/…), или «» — сайт свой."""
    host = host_of(url)
    labels = host.split(".")
    for i in range(len(labels) - 1):
        tail = ".".join(labels[i:])
        if tail in FOREIGN_HOSTS:
            return tail
    return ""


def is_own_site(url: str) -> bool:
    """Похоже на собственный сайт компании: есть домен и это не соцсеть, справочник или агрегатор."""
    host = host_of(url)
    return "." in host and not foreign_host(url)


def clean_site(url: str) -> str:
    return (url or "").split("?")[0].rstrip("/")


def shared_hosts(items: list[dict]) -> set[str]:
    """Справочники, которых нет в списке: один домен стоит в карточках компаний с разными названиями, и везде это
    внутренняя страница. У сети филиалов название одно, у справочника — разные. Если хоть одна карточка ведёт
    на главную страницу домена, это чей-то собственный сайт (например, общий сайт сети с разными вывесками)."""
    seen: dict[str, set[str]] = {}
    own: set[str] = set()
    for item in items:
        for url in item.get("urls") or []:
            parts = urlsplit(url if "//" in url else "//" + url)
            host = host_of(url)
            if not parts.path.strip("/") or any(host.endswith("." + x) for x in OWN_ON_SUBDOMAIN):
                own.add(host)
            elif host:
                seen.setdefault(host, set()).add(" ".join((item.get("title") or "").lower().split()))
    return {host for host, titles in seen.items() if len(titles) > 1 and host not in own}


def company_site(item: dict, shared: set[str] | frozenset[str] = frozenset()) -> tuple[str, str]:
    """(собственный сайт компании, чужая площадка вместо сайта) по карточке с карт. Обе строки могут быть пустыми."""
    foreign = ""
    for url in item.get("urls") or []:
        host = host_of(url)
        if is_own_site(url) and host not in shared:
            return clean_site(url), ""
        foreign = foreign or foreign_host(url) or host
    return "", foreign
