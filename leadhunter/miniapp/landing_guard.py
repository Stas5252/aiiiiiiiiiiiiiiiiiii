"""Защита публичной формы лендинга: ограничение частоты, одноразовый токен формы, склейка повторных заявок.

Всё хранится в памяти процесса: после перезапуска счётчики и список использованных токенов обнуляются.
"""
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from collections import deque
from collections.abc import Callable

from aiohttp import web

# Принятые заявки: с одного адреса и на одну страницу
IP_LIMIT, IP_WINDOW = 3, 10 * 60
SLUG_LIMIT, SLUG_WINDOW = 30, 60 * 60
# Любые отправки формы, включая отклонённые: чтобы не перебирали токены и не грузили базу
ATTEMPT_LIMIT, ATTEMPT_WINDOW = 30, 10 * 60
TOKEN_TTL = 2 * 60 * 60
# Повторная заявка с того же контакта на ту же страницу за это время не создаёт второй лид
MERGE_WINDOW = 24 * 60 * 60


# Откуда может прийти запрос от своего прокси: эта же машина и частные сети (Docker, локальная сеть)
PROXY_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7")]


class RateLimiter:
    """Скользящее окно: не больше limit событий за window секунд на ключ."""

    def __init__(self, limit: int, window: float, now: Callable[[], float] = time.monotonic, max_keys: int = 20000):
        self.limit, self.window, self.now, self.max_keys = limit, window, now, max_keys
        self._hits: dict[str, deque[float]] = {}

    def _fresh(self, key: str) -> deque[float]:
        t = self.now()
        q = self._hits.get(key)
        if q is None:
            if len(self._hits) >= self.max_keys:
                self._prune(t)
            q = self._hits[key] = deque()
        while q and t - q[0] >= self.window:
            q.popleft()
        return q

    def _prune(self, t: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or t - q[-1] >= self.window]:
            del self._hits[k]
        if len(self._hits) >= self.max_keys:  # все ключи живые — это уже атака, начинаем с чистого листа
            self._hits.clear()

    def take(self, key: str) -> bool:
        """Засчитывает событие, если лимит не исчерпан. Без await внутри — параллельные запросы не проскочат."""
        q = self._fresh(key)
        if len(q) >= self.limit:
            return False
        q.append(self.now())
        return True

    def retry_after(self, key: str) -> int:
        q = self._fresh(key)
        return max(1, int(self.window - (self.now() - q[0])) + 1) if q else 1


class FormTokens:
    """Токен формы: отметка времени + случайное число + подпись, привязан к странице, принимается один раз."""

    def __init__(self, secret: str, ttl: int = TOKEN_TTL, now: Callable[[], float] = time.time):
        self.key = hmac.new(b"LandingForm", (secret or secrets.token_hex(32)).encode(), hashlib.sha256).digest()
        self.ttl, self.now = ttl, now
        self._used: dict[str, float] = {}

    def _sign(self, slug: str, ts: str, nonce: str) -> str:
        return hmac.new(self.key, f"{slug}.{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]

    def issue(self, slug: str) -> str:
        ts, nonce = str(int(self.now())), secrets.token_urlsafe(9)
        return f"{ts}.{nonce}.{self._sign(slug, ts, nonce)}"

    def valid(self, slug: str, token: str) -> bool:
        """Подпись верна, срок не вышел, токен ещё не использован."""
        parts = token.split(".") if isinstance(token, str) else []
        if len(parts) != 3 or not parts[0].isdigit() or len(parts[0]) > 12:
            return False
        ts, nonce, sig = parts
        if not hmac.compare_digest(self._sign(slug, ts, nonce), sig):
            return False
        age = self.now() - int(ts)
        return -60 <= age <= self.ttl and nonce not in self._used

    def consume(self, token: str) -> None:
        now = self.now()
        if len(self._used) > 5000:
            self._used = {n: exp for n, exp in self._used.items() if exp > now}
        self._used[token.split(".")[1]] = now + self.ttl + 60


def client_ip(request: web.Request) -> str:
    """Адрес посетителя. Заголовкам прокси верим, только если запрос пришёл с этой же машины или из частной сети
    (cloudflared, nginx): тогда настоящий адрес — последний в X-Forwarded-For, его дописывает сам прокси."""
    peer = _ip(request.remote)
    if peer is None or any(peer in net for net in PROXY_NETS):
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        via = _ip(forwarded) or _ip(request.headers.get("CF-Connecting-IP", "").strip())
        if via is not None:
            peer = via
    if peer is None:
        return "unknown"
    if peer.version == 6:  # у одного абонента целая подсеть /64
        return str(ipaddress.ip_network(f"{peer}/64", strict=False).network_address)
    return str(peer)


def _ip(value: str | None):
    try:
        ip = ipaddress.ip_address(value or "")
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if ip.version == 6 else ip


def contact_key(contact: str) -> str:
    """Один и тот же контакт, записанный по-разному: «+7 (900) 111-22-33» = «89001112233», «@Anna» = «anna»."""
    c = contact.strip().lower()
    digits = re.sub(r"\D", "", c)
    if len(digits) >= 10 and not re.search(r"[a-zа-яё@]", c):
        return digits[-10:]
    return re.sub(r"^(https?://)?(t\.me/|@)", "", re.sub(r"\s+", "", c))


class LandingGuard:
    def __init__(self, secret: str):
        self.tokens = FormTokens(secret)
        self.per_ip = RateLimiter(IP_LIMIT, IP_WINDOW)
        self.per_slug = RateLimiter(SLUG_LIMIT, SLUG_WINDOW)
        self.attempts = RateLimiter(ATTEMPT_LIMIT, ATTEMPT_WINDOW)
