"""Мелочи для ответов бота: длинный текст — частями, числа из команд — без падений."""
from aiogram.types import Message

TG_LIMIT = 4000  # у Telegram 4096 на сообщение; запас — на эмодзи и разметку


def _tg_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2  # Telegram считает длину в единицах UTF-16


def _cut(line: str, limit: int) -> int:
    """Где резать слишком длинную строку, чтобы не разорвать тег или &сущность;."""
    n = limit
    while _tg_len(line[:n]) > limit:
        n -= max(1, (_tg_len(line[:n]) - limit) // 2)
    head = line[:n]
    for opener, closer in (("<", ">"), ("&", ";")):
        i = head.rfind(opener)
        if i > 0 and closer not in head[i:]:
            n = i
            head = line[:n]
    return max(n, 1)


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет текст на части не длиннее limit по границам строк."""
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while _tg_len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            n = _cut(line, limit)
            parts.append(line[:n])
            line = line[n:]
        if cur and _tg_len(cur) + 1 + _tg_len(line) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur.strip():
        parts.append(cur)
    return parts


async def answer_long(m: Message, text: str, **kwargs) -> None:
    for part in split_text(text):
        await m.answer(part, **kwargs)


def parse_int(raw: str | None, lo: int, hi: int) -> int | None:
    """Целое из аргумента команды. None — если это не обычное число в пределах lo…hi («²», «99999999999» и т. п.)."""
    raw = (raw or "").strip()
    if not raw or not raw.isascii() or not raw.isdigit() or len(raw) > 15:
        return None
    n = int(raw)
    return n if lo <= n <= hi else None
