"""Рендер слайдов, обложек и кадров для видео на Pillow — без сети."""
import io
import re
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/truetype/liberation",
         "/usr/share/fonts/truetype/noto", "/usr/share/fonts/noto", "/usr/share/fonts/TTF",
         "/Library/Fonts", "/System/Library/Fonts/Supplemental", "C:/Windows/Fonts")
_NAMES = {False: ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf", "Arial.ttf", "arial.ttf"),
          True: ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf")}


HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def valid_color(c) -> bool:
    """Цвет бренда — только #RRGGBB: другое Pillow либо не понимает, либо красит не так, как ждёт владелец."""
    return isinstance(c, str) and bool(HEX_COLOR.match(c.strip()))


@dataclass
class Brand:
    name: str = ""
    handle: str = ""
    primary: str = "#14141F"
    accent: str = "#7C5CFF"
    text: str = "#FFFFFF"

    def __post_init__(self):
        # неверный цвет (из старой настройки или кабинета) не должен ронять рендер — берём цвет по умолчанию
        for key in ("primary", "accent", "text"):
            value = getattr(self, key)
            setattr(self, key, value.strip() if valid_color(value) else type(self).__dataclass_fields__[key].default)
        self.name = str(self.name or "")
        self.handle = str(self.handle or "")


@lru_cache(maxsize=None)
def find_font(bold: bool = False) -> str | None:
    import os
    for name in _NAMES[bold]:
        for d in _DIRS:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


@lru_cache(maxsize=256)
def load_font(size: int, bold: bool = False):
    path = find_font(bold)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


_INVISIBLE = re.compile("[\u200b-\u200d\u2060\ufe00-\ufe0f\u20e3\U0001F3FB-\U0001F3FF\U000E0020-\U000E007F]")


@lru_cache(maxsize=4096)
def _has_glyph(ch: str, bold: bool) -> bool:
    """Есть ли символ в шрифте: у отсутствующих (эмодзи) рисунок совпадает с «квадратом»-заглушкой."""
    font = load_font(40, bold)
    try:
        blank, mask = font.getmask("\U000F0000"), font.getmask(ch)
    except Exception:
        return True
    return (mask.size, bytes(mask)) != (blank.size, bytes(blank))


def printable(text, bold: bool = False) -> str:
    """Текст для картинки: без эмодзи и значков, которых нет в шрифте (иначе на слайде пустые квадраты)."""
    text = _INVISIBLE.sub("", str(text or ""))
    if text.isascii():
        return text
    out = "".join(ch for ch in text if ord(ch) < 0x2000 or _has_glyph(ch, bold))
    if out == text:
        return text
    out = re.sub(r"[ \t]{2,}", " ", out)
    return "\n".join(line.strip() for line in out.split("\n"))


def wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            # слово длиннее строки режем по символам
            while draw.textlength(word, font=font) > max_width and len(word) > 1:
                lo, hi = 1, len(word)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if draw.textlength(word[:mid], font=font) <= max_width:
                        lo = mid
                    else:
                        hi = mid - 1
                k = lo
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(word[:k])
                word = word[k:]
            test = f"{cur} {word}".strip()
            if cur and draw.textlength(test, font=font) > max_width:
                lines.append(cur)
                cur = word
            else:
                cur = test
        lines.append(cur)
    return lines


def _mix(fg: str, bg: str, a: float) -> tuple[int, int, int]:
    f, b = _rgb(fg), _rgb(bg)
    return tuple(int(f[i] * a + b[i] * (1 - a)) for i in range(3))


def _rgb(c: str) -> tuple[int, int, int]:
    if not valid_color(c):
        c = Brand.primary
    c = c.strip().lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _fit(draw, text, bold, max_w, max_h, start, min_size, spacing=1.25):
    """Подбирает шрифт и обрезает «…», чтобы текст влез в max_w × max_h."""
    text = printable(text, bold).strip()[:1500]  # остальное всё равно уйдёт в «…»
    size = start
    while True:
        font = load_font(size, bold)
        lines = wrap_text(draw, text, font, max_w)
        lh = int(size * spacing)
        fits = len(lines) * lh <= max_h
        if fits or size <= min_size:
            break
        size -= 2
    cap = max(1, max_h // lh)
    if len(lines) > cap:
        lines = lines[:cap]
        last = lines[-1].rstrip()
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return font, lines, lh


def _draw_lines(draw, lines, font, x, y, lh, fill, center_w=0):
    for ln in lines:
        dx = (center_w - draw.textlength(ln, font=font)) / 2 if center_w else 0
        draw.text((x + dx, y), ln, font=font, fill=fill)
        y += lh
    return y


def _canvas(brand: Brand, size):
    img = Image.new("RGB", size, brand.primary)
    d = ImageDraw.Draw(img)
    m = int(size[0] * 0.085)
    d.rectangle((m, int(size[1] * 0.045), m + int(size[0] * 0.12), int(size[1] * 0.045) + 10), fill=brand.accent)
    return img, d, m


def _footer(d, brand: Brand, size, m):
    font = load_font(26)
    col = _mix(brand.text, brand.primary, 0.5)
    y = size[1] - int(size[1] * 0.05) - 30
    name, handle = printable(brand.name), printable(brand.handle)
    if name:
        d.text((m, y), name, font=font, fill=col)
    if handle:
        d.text((size[0] - m - d.textlength(handle, font=font), y), handle, font=font, fill=col)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def render_slide(title: str, body: str, index: int, total: int, brand: Brand, size=(1080, 1350)) -> bytes:
    title, body = printable(title, True), printable(body)
    img, d, m = _canvas(brand, size)
    w, h = size
    cover = index == 1
    if total > 1 and not cover:
        f = load_font(34, True)
        t = f"{index}/{total}"
        d.text((w - m - d.textlength(t, font=f), int(h * 0.04)), t, font=f, fill=brand.accent)
    max_w, top, bottom = w - 2 * m, int(h * 0.16), int(h * 0.88)
    if cover:
        font, lines, lh = _fit(d, title, True, max_w, int((bottom - top) * 0.6), 88, 48, 1.2)
        body_h = bottom - top - len(lines) * lh - 60
    else:
        font, lines, lh = _fit(d, title, True, max_w, int((bottom - top) * 0.3), 64, 40, 1.2)
        body_h = bottom - top - len(lines) * lh - 50
    y = top
    if cover:
        y = top + max(0, (bottom - top - len(lines) * lh - (200 if body.strip() else 0)) // 3)
    y = _draw_lines(d, lines, font, m, y, lh, brand.text)
    if body.strip() and body_h > 60:
        y += 50
        bf, bl, blh = _fit(d, body, False, max_w, min(body_h, bottom - y), 40, 26, 1.4)
        _draw_lines(d, bl, bf, m, y, blh, _mix(brand.text, brand.primary, 0.85))
    _footer(d, brand, size, m)
    return _png(img)


def render_carousel(slides: list[dict], brand: Brand, size=(1080, 1350)) -> list[bytes]:
    n = len(slides)
    slides = [s if isinstance(s, dict) else {"title": "", "text": s} for s in slides or []]
    return [render_slide(str(s.get("title") or ""), str(s.get("text") or ""), i, n, brand, size)
            for i, s in enumerate(slides, 1)]


COVER_SIZE = (1080, 1350)  # 4:5 — как слайды карусели: целиком встаёт в ленту Instagram, Threads, VK и Telegram


def same_line(a: str, b: str) -> bool:
    """Одна и та же строка с точностью до регистра, пробелов и знаков по краям."""
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", str(s or "")).strip(" .!?…—-:;,«»\"'").lower()
    a, b = norm(a), norm(b)
    return bool(a) and (a == b or a.startswith(b) or b.startswith(a)) if b else False


def render_cover(title: str, subtitle: str, brand: Brand, size=COVER_SIZE) -> bytes:
    if same_line(title, subtitle):
        subtitle = ""  # подзаголовок не должен повторять заголовок
    return render_slide(title or "", subtitle or "", 1, 1, brand, size)


def render_scene(text: str, brand: Brand, size=(1080, 1920), subtitle: str = "") -> bytes:
    text, subtitle = printable(text, True), printable(subtitle)
    img, d, m = _canvas(brand, size)
    w, h = size
    max_w = w - 2 * m
    sub_h = int(h * 0.12) if subtitle.strip() else 0
    font, lines, lh = _fit(d, text, True, max_w, int(h * 0.6) - sub_h, 84, 44, 1.25)
    total_h = len(lines) * lh + (sub_h + 20 if sub_h else 0)
    y = (h - total_h) // 2
    y = _draw_lines(d, lines, font, m, y, lh, brand.text, center_w=max_w)
    if sub_h:
        sf, sl, slh = _fit(d, subtitle, False, max_w, sub_h, 40, 26, 1.3)
        _draw_lines(d, sl, sf, m, y + 20, slh, brand.accent, center_w=max_w)
    _footer(d, brand, size, m)
    return _png(img)
