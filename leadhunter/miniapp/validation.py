"""Единый разбор входных данных кабинета: неверный ввод — это 400 с понятным текстом, а не 500."""
import json
import re
from urllib.parse import urlsplit

from aiohttp import web

COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
SQLITE_MAX_INT = 2**63 - 1
SAFE_SCHEMES = ("http", "https", "tg")


def bad(text: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=text)


def to_int(value, name: str, lo: int | None = None, hi: int | None = None) -> int:
    """Целое из строки запроса или JSON. bool, дроби и мусор — 400; выход за границы прижимается к ним."""
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise bad(f"{name}: нужно целое число")
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        raise bad(f"{name}: нужно целое число")
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def to_float(value, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool):
        raise bad(f"{name}: нужно число")
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise bad(f"{name}: нужно число")
    if x != x or x in (float("inf"), float("-inf")):
        raise bad(f"{name}: нужно число")
    return max(lo, min(hi, x))


def query_int(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    raw = request.query.get(name)
    return default if raw in (None, "") else to_int(raw, name, lo, hi)


def path_id(request: web.Request, name: str = "id") -> int:
    """id из адреса. Слишком длинное число не помещается в SQLite — такой записи точно нет."""
    n = int(request.match_info[name])
    if n > SQLITE_MAX_INT:
        raise web.HTTPNotFound()
    return n


async def json_object(request: web.Request) -> dict:
    """Тело запроса — только JSON-объект с Content-Type: application/json.
    Проверка типа содержимого закрывает и подделку запросов с чужих сайтов: такой запрос браузер без разрешения не отправит."""
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="нужен Content-Type: application/json")
    try:
        body = json.loads(await request.text())
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise bad("тело запроса — не JSON")
    if not isinstance(body, dict):
        raise bad("тело запроса должно быть JSON-объектом")
    return body


def to_str(value, name: str, max_len: int = 5000) -> str:
    if not isinstance(value, str):
        raise bad(f"{name}: нужна строка")
    return value.strip()[:max_len]


def to_bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise bad(f"{name}: нужно true или false")
    return value


def to_dict(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise bad(f"{name}: нужен объект")
    return value


def to_str_list(value, name: str, max_items: int = 200, max_len: int = 200) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise bad(f"{name}: нужен список строк")
    return [x.strip()[:max_len] for x in value[:max_items]]


def to_color(value, name: str) -> str:
    """Пустая строка — «не задано», иначе строго #RRGGBB: другое ломает рендер картинок и видео."""
    v = to_str(value, name, 20)
    if v and not COLOR_RE.fullmatch(v):
        raise bad(f"{name}: цвет в формате #RRGGBB, например #14141F")
    return v


def safe_url(url) -> str:
    """Ссылки в кабинет отдаём только http(s) и tg: — javascript: и прочее превращается в пустую строку."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in SAFE_SCHEMES else ""
