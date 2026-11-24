"""Проверка подписи Telegram Mini App initData (HMAC-SHA256, ключ = HMAC("WebAppData", bot_token))."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_CLOCK_SKEW = 60


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict | None:
    """Возвращает словарь user при валидной подписи, иначе None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        age = time.time() - int(pairs.get("auth_date", "0"))
        # Дата из будущего (с запасом на расхождение часов) — такой initData Telegram выдать не мог
        if age < -MAX_CLOCK_SKEW or (max_age and age > max_age):
            return None
        user = json.loads(pairs.get("user", "{}"))
    except (ValueError, json.JSONDecodeError):
        return None
    return user if isinstance(user, dict) else None


def sign_init_data(fields: dict, bot_token: str) -> str:
    """Для тестов: собирает initData с правильной подписью."""
    from urllib.parse import urlencode

    data = {k: (json.dumps(v, separators=(",", ":"), ensure_ascii=False) if isinstance(v, dict) else str(v))
            for k, v in fields.items()}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)
