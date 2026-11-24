"""Бриф исполнителя: кто, что продаёт, почём, кому, где, кейсы, тон. На него опирается весь ИИ —
оценка лида, отклик и контент. Хранится одной настройкой `brief` (JSON); пока бриф пуст, работает
старое описание /profile."""
import asyncio
import html
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .extract import budget_rub


@dataclass(frozen=True)
class Field:
    key: str
    title: str
    question: str
    example: str
    limit: int = 1500


FIELDS = (
    Field("who", "Кто вы", "Как вас зовут или как называется студия? Вы один или команда?",
          "Стас, делаю сайты и ботов один"),
    Field("services", "Услуги", "Что именно вы продаёте? Перечислите услуги.",
          "Лендинги на Tilda, Telegram-боты, автоматизация заявок"),
    Field("prices", "Цены", "Сколько стоит каждая услуга — «от»?", "Лендинг от 25 000 ₽, бот от 15 000 ₽"),
    Field("min_budget", "Минимальный заказ, ₽",
          "Ниже какой суммы заказ вам не интересен? Числом в рублях. 0 — без ограничения.", "10000", 12),
    Field("client", "Идеальный клиент", "Кто ваш клиент и из каких он ниш?",
          "Малый бизнес: салоны, клиники, автосервисы — без сайта или со старым"),
    Field("geo", "География", "В каких городах и странах работаете? Удалённо берёте?",
          "Саратов — лично, вся Россия — удалённо"),
    Field("cases", "Кейсы", "2–3 работы: что сделали, какой результат, ссылка.",
          "Сайт стоматологии: заявок стало 40 в месяц вместо 12 — example.ru", 3000),
    Field("edge", "Чем вы лучше", "Почему выбирают вас, а не других?",
          "Сдаю за 5 дней, правки бесплатно месяц, сам на связи"),
    Field("tone", "Тон общения", "На «ты» или на «вы»? Коротко или подробно? Можно вставить пример вашего отклика.",
          "На «вы», коротко, без канцелярита"),
    Field("stop", "Что не берёте", "Какие темы и заказы не берёте?", "Казино, ставки, доработка чужого кода"),
    Field("contact", "Куда вести клиента", "Что указать в конце отклика: Telegram, сайт, созвон?", "Telegram @stas"),
)
KEYS = tuple(f.key for f in FIELDS)
BY_KEY = {f.key: f for f in FIELDS}
SITE_BYTES = 400_000
SITE_REDIRECTS = 3
FILL_SYSTEM = """Ты помогаешь исполнителю (фрилансеру или студии) заполнить бриф о себе.
По тексту ниже разложи сведения по полям. Бери только то, что прямо сказано в тексте: ничего не выдумывай,
неизвестное поле оставь пустой строкой. Пиши по-русски, коротко, от первого лица.
Ответь строго JSON без пояснений:
{{{fields}}}"""


class BriefError(ValueError):
    """Ошибка, текст которой можно показать человеку."""


def clean(raw: dict) -> dict[str, str]:
    """Только известные поля, строки обрезаны; минимальный заказ — целое число рублей."""
    out = {}
    for key, value in raw.items():
        f = BY_KEY.get(key)
        if f is None:
            raise BriefError(f"нет такого поля брифа: {key}")
        if value is None:
            value = ""
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise BriefError(f"{f.title}: нужен текст")
        value = str(value).strip()
        if key == "min_budget":
            digits = re.sub(r"[\s\u00a0₽]|руб\w*\.?|р\.", "", value.lower())
            if digits in ("", "нет", "0"):
                value = ""
            elif not digits.isdigit() or len(digits) > 9:
                raise BriefError("Минимальный заказ: нужно число в рублях, например 10000")
            else:
                value = str(int(digits))
        out[key] = value[:f.limit]
    return out


async def load(db) -> dict[str, str]:
    try:
        data = json.loads(await db.get_setting("brief", "") or "{}")
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    return {k: str(data.get(k) or "") for k in KEYS}


async def save(db, patch: dict) -> dict[str, str]:
    """Дописывает поля к сохранённому брифу; пустая строка очищает поле."""
    data = {**await load(db), **clean(patch)}
    await db.set_setting("brief", json.dumps({k: v for k, v in data.items() if v}, ensure_ascii=False))
    return data


def filled(data: dict) -> int:
    return sum(1 for k in KEYS if data.get(k))


def min_budget(data: dict) -> int:
    return int(data["min_budget"]) if (data.get("min_budget") or "").isdigit() else 0


def text(data: dict, fallback: str = "") -> str:
    """Бриф одним текстом для ИИ. Услуги не заполнены — вместо них старое описание «что продаём»."""
    if not filled(data):
        return fallback
    lines = []
    for f in FIELDS:
        value = data.get(f.key) or ""
        if f.key == "services" and not value:
            value = fallback
        if f.key == "min_budget":
            value = f"{value} ₽ — заказы дешевле не берём" if value else ""
        if value:
            lines.append(f"{f.title.split(',')[0]}: {value}")
    return "\n".join(lines)


async def profile(db, default: str = "") -> str:
    """Что видит ИИ об исполнителе: бриф, а пока он пуст — описание из /profile."""
    old = await db.get_setting("profile", default) or default
    return text(await load(db), old)


def below_min(lead_text: str, data: dict) -> bool:
    """Сумма в тексте заказа точно ниже минимального. Нет суммы, валюта или «за час» — не отсеиваем."""
    low = min_budget(data)
    amount = budget_rub(lead_text) if low else None
    return amount is not None and amount < low


async def fill(llm, source: str) -> dict[str, str]:
    """ИИ раскладывает свободный текст о себе по полям брифа. Ничего не сохраняет — человек проверит и поправит."""
    fields = ", ".join(f'"{f.key}": "{f.title}: {f.question}"' for f in FIELDS)
    data = await llm.complete_json(FILL_SYSTEM.format(fields=fields), source[:12000], temperature=0)
    if not isinstance(data, dict):
        raise BriefError("ИИ ответил не по форме — попробуйте ещё раз")
    out = {}
    for key in KEYS:
        value = data.get(key)
        if isinstance(value, list):
            value = "; ".join(str(v) for v in value)
        try:
            out.update(clean({key: "" if value is None else str(value)}))
        except BriefError:
            out[key] = ""  # «от 10 тысяч» вместо числа — оставляем человеку
    return out


# --- текст сайта для «Заполнить за меня» ---
_TAG_BLOCK = re.compile(r"(?is)<(script|style|noscript|svg|template)\b.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
_URL = re.compile(r"(?i)^(?:https?://)?(?:[a-z0-9а-яё-]+\.)+[a-zа-яё]{2,}(?::\d+)?(?:/\S*)?$")


def looks_like_url(value: str) -> bool:
    return bool(_URL.match(value.strip()))


def html_to_text(page: str) -> str:
    page = _TAG.sub(" ", _TAG_BLOCK.sub(" ", page))
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


async def _public_ip(host: str, port: int) -> str:
    """Адрес сайта в интернете. Локальные и внутренние адреса не открываем — иначе через ссылку
    можно было бы читать службы на самой машине с ботом."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise BriefError("Сайт не найден — проверьте ссылку") from None
    ips = [info[4][0] for info in infos]
    if not ips or not all(ipaddress.ip_address(ip.split("%")[0]).is_global for ip in ips):
        raise BriefError("Эту ссылку открыть нельзя: адрес не из интернета")
    return ips[0]


async def site_text(url: str, *, proxy: str | None = None, timeout: int = 15) -> str:
    """Текст главной страницы сайта. Каждый адрес (и после переадресации) проверяется и закрепляется
    за проверенным IP — подменить его между проверкой и запросом нельзя."""
    from curl_cffi import CurlOpt
    from curl_cffi.requests import AsyncSession

    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    for _ in range(SITE_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise BriefError("Нужна обычная ссылка на сайт: https://example.ru")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ip = await _public_ip(parts.hostname, port)
        pin = f"{parts.hostname}:{port}:{'[' + ip + ']' if ':' in ip else ip}"
        try:
            async with AsyncSession(impersonate="chrome", proxy=proxy, timeout=timeout,
                                    curl_options={CurlOpt.RESOLVE: [pin]}) as s:
                r = await s.get(url, allow_redirects=False, stream=True)
                try:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        raise BriefError(f"Сайт отдаёт ошибку {r.status_code}")
                    body = b""
                    async for chunk in r.aiter_content():
                        body += chunk
                        if len(body) >= SITE_BYTES:
                            break
                finally:
                    await r.aclose()
        except BriefError:
            raise
        except Exception as e:
            raise BriefError(f"Сайт не открывается ({type(e).__name__})") from None
        page = html_to_text(body[:SITE_BYTES].decode(r.encoding or "utf-8", "replace"))
        if len(page) < 200:
            raise BriefError("На странице почти нет текста — вставьте описание вручную")
        return page
    raise BriefError("Слишком много переадресаций — вставьте текст вручную")


async def fill_from(llm, source: str, *, proxy: str | None = None) -> dict[str, str]:
    """source — текст о себе или ссылка на сайт."""
    source = source.strip()
    if len(source) < 20 and not looks_like_url(source):
        raise BriefError("Пришлите текст о себе (хотя бы пару предложений) или ссылку на сайт")
    if looks_like_url(source):
        source = "Текст с сайта исполнителя:\n" + await site_text(source, proxy=proxy)
    return await fill(llm, source)
