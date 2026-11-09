"""Мультик кодом: плоские 2D-персонажи на Pillow, кадры летят в ffmpeg."""
import asyncio
import math
import os
import random
import re
import shutil
import tempfile
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw

from .render import Brand, _mix, _rgb, load_font, wrap_text
from .video import PAD, ffmpeg_available, probe_duration

STYLES = ("flat", "chalk")
SHAPES = ("round", "tall", "square")
EMOTIONS = ("neutral", "happy", "angry", "sad", "surprised", "thinking")
ACTIONS = ("idle", "talk", "walk", "jump", "point", "shake", "nod")
POSITIONS = ("left", "center", "right")
BGS = ("office", "street", "home", "abstract", "none")
MIN_DUR, MAX_DUR = 1.5, 8.0
MAX_SCENES, MAX_CHARS, MAX_SCENE_CHARS = 12, 4, 3
TRANSITION = 0.3
WALK_T = 1.1
CS = 1.15  # масштаб персонажей относительно опорной сетки 1080×1920
FEET_Y = 0.75
_PALETTE = ("#F2C14E", "#6FB9F0", "#F08A6F", "#8ED1A0")
_HAIR = ("#3B2A20", "#1F2430", "#8A4B2D", "#C98F2E")
_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
CHALK_BG = "#1B211E"


@dataclass
class ToonResult:
    path: str
    duration: float
    scenes: int


# ───────────────────────── нормализация сценария ─────────────────────────

def _color(v, default: str) -> str:
    m = _HEX.match(str(v or "").strip())
    if not m:
        return default
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


def _num(v, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def normalize_script(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("сценарий должен быть объектом")
    style = data.get("style") if data.get("style") in STYLES else "flat"

    raw_chars = data.get("characters") or {}
    if isinstance(raw_chars, list):
        raw_chars = {str(c.get("id") or f"c{i}"): c for i, c in enumerate(raw_chars) if isinstance(c, dict)}
    chars: dict[str, dict] = {}
    if isinstance(raw_chars, dict):
        for i, (cid, c) in enumerate(raw_chars.items()):
            if len(chars) >= MAX_CHARS:
                break
            c = c if isinstance(c, dict) else {}
            cid = str(cid)
            chars[cid] = {"name": str(c.get("name") or cid)[:24],
                          "color": _color(c.get("color"), _PALETTE[i % len(_PALETTE)]),
                          "shape": c.get("shape") if c.get("shape") in SHAPES else SHAPES[i % len(SHAPES)]}

    scenes = []
    for sc in data.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        line = sc.get("line")
        if isinstance(line, str):
            line = {"speaker": None, "text": line}
        text = str((line or {}).get("text") or "").strip()[:240] if isinstance(line, dict) else ""
        line = {"speaker": str(line.get("speaker")) if line.get("speaker") else None, "text": text} if text else None

        placed, taken = [], set()
        for c in sc.get("characters") or []:
            if not isinstance(c, dict) or str(c.get("id")) not in chars or len(placed) >= MAX_SCENE_CHARS:
                continue
            pos = c.get("pos") if c.get("pos") in POSITIONS else "center"
            if pos in taken:
                free = [p for p in ("center", "left", "right") if p not in taken]
                if not free:
                    continue
                pos = free[0]
            taken.add(pos)
            action = c.get("action") if c.get("action") in ACTIONS else "idle"
            if action == "idle" and line and line["speaker"] == str(c["id"]):
                action = "talk"
            placed.append({"id": str(c["id"]), "pos": pos, "action": action,
                           "emotion": c.get("emotion") if c.get("emotion") in EMOTIONS else "neutral"})
        if not placed and not line:
            continue
        audio = sc.get("audio")
        scenes.append({"bg": sc.get("bg") if sc.get("bg") in BGS else "abstract",
                       "duration": min(MAX_DUR, max(MIN_DUR, _num(sc.get("duration"), 4.0))),
                       "characters": placed, "line": line,
                       "caption": str(sc.get("caption") or "").strip()[:90],
                       "sfx": sc.get("sfx"),
                       "audio": bytes(audio) if isinstance(audio, (bytes, bytearray)) and audio else None})
        if len(scenes) >= MAX_SCENES:
            break
    if not scenes:
        raise ValueError("в сценарии нет пригодных сцен")
    return {"title": str(data.get("title") or "")[:120], "style": style, "characters": chars,
            "scenes": scenes, "_normalized": True}


# ───────────────────────── утилиты ─────────────────────────

def _ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def _lum(c) -> float:
    r, g, b = c
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def _shade(c: str, k: float) -> str:
    return "#%02X%02X%02X" % tuple(int(v * k) for v in _rgb(c))


class _Pen:
    """Рисует в одном стиле: flat — заливка, chalk — белые двойные штрихи по тёмному."""

    def __init__(self, img: Image.Image, style: str, k: float, seed: int = 0):
        self.d = ImageDraw.Draw(img)
        self.chalk = style == "chalk"
        self.k = k
        self.ink = "#F4F4EF" if self.chalk else "#1F2430"
        self.bg = CHALK_BG
        self.ow = max(1, round((5 if self.chalk else 6) * k))
        self.rng = random.Random(seed)

    def fill(self, c: str):
        return _mix(c, self.bg, 0.22) if self.chalk else c

    def _j(self) -> float:
        return self.rng.uniform(-2, 2) * self.k if self.chalk else 0

    def shape(self, kind: str, box, fill: str, r: float = 0, outline: bool = True, double: bool = False):
        d, ow = self.d, self.ow
        fn = d.ellipse if kind == "ell" else None
        kw = {"fill": self.fill(fill)}
        if outline or self.chalk:
            kw.update(outline=self.ink, width=ow)
        if fn:
            fn(box, **kw)
        else:
            d.rounded_rectangle(box, max(0, r), **kw)
        if self.chalk and double:
            j = self._j(), self._j()
            b2 = (box[0] + j[0], box[1] + j[1], box[2] + j[0], box[3] + j[1])
            if fn:
                fn(b2, outline=self.ink, width=max(1, ow // 2))
            else:
                d.rounded_rectangle(b2, max(0, r), outline=self.ink, width=max(1, ow // 2))

    def poly(self, pts, fill: str, outline: bool = True):
        kw = {"fill": self.fill(fill)}
        if outline or self.chalk:
            kw.update(outline=self.ink, width=self.ow)
        self.d.polygon(pts, **kw)

    def line(self, p0, p1, w: float, color: str | None = None):
        w = max(1, round(w))
        c = color or self.ink
        self.d.line([p0, p1], fill=c, width=w)
        if w > 3:
            r = w / 2 - 0.5
            for p in (p0, p1):
                self.d.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r), fill=c)

    def limb(self, p0, p1, w: float, color: str):
        if self.chalk:
            self.line(p0, p1, self.ow * 1.6)
        else:
            self.line(p0, p1, w + 2 * self.ow, self.ink)
            self.line(p0, p1, w, color)


# ───────────────────────── фоны ─────────────────────────

def _bg_base(bg: str, style: str, brand: Brand, size) -> Image.Image:
    w, h = size
    if style == "chalk":
        return Image.new("RGB", size, CHALK_BG)
    if bg == "abstract":
        grad = Image.linear_gradient("L").resize(size)
        top, bot = _rgb(brand.primary), _mix(brand.accent, brand.primary, 0.55)
        return Image.composite(Image.new("RGB", size, bot), Image.new("RGB", size, top), grad)
    wall = {"office": "#E9EEF5", "street": "#BFE3F7", "home": "#F3E3CF"}.get(bg)
    return Image.new("RGB", size, wall or brand.primary)


def _draw_bg(img: Image.Image, bg: str, style: str, size):
    if bg in ("none", "abstract"):
        return
    w, h = size
    sx, sy = w / 1080, h / 1920
    pen = _Pen(img, style, min(sx, sy), seed=7)

    def B(x0, y0, x1, y1):
        return (x0 * sx, y0 * sy, x1 * sx, y1 * sy)

    def rect(box, fill, r=0):
        pen.shape("rect", B(*box), fill, r * min(sx, sy), outline=False, double=True)

    def ell(box, fill):
        pen.shape("ell", B(*box), fill, outline=False, double=True)

    def poly(pts, fill):
        pen.poly([(x * sx, y * sy) for x, y in pts], fill, outline=False)

    if bg == "office":
        rect((-20, 1230, 1100, 1930), "#D7C4A3")
        rect((-20, 1200, 1100, 1240), "#B59E7B")
        rect((700, 380, 1010, 820), "#FFFFFF")
        rect((724, 404, 986, 796), "#BFE3F7")
        ell((780, 450, 860, 530), "#FFE08A")
        rect((846, 404, 864, 796), "#FFFFFF")
        rect((724, 590, 986, 608), "#FFFFFF")
        ell((90, 400, 250, 560), "#FFFFFF")
        pen.line((170 * sx, 480 * sy), (170 * sx, 420 * sy), 8 * sx)
        pen.line((170 * sx, 480 * sy), (215 * sx, 500 * sy), 8 * sx)
        rect((60, 1040, 720, 1090), "#9B6B43")
        rect((90, 1090, 130, 1230), "#7E5535")
        rect((650, 1090, 690, 1230), "#7E5535")
        rect((250, 860, 530, 1030), "#2B3140", 16)
        rect((268, 878, 512, 1012), "#7FC4F0", 8)
        rect((378, 1030, 402, 1040), "#2B3140")
        rect((330, 1030, 450, 1044), "#2B3140", 6)
        rect((940, 1120, 1030, 1230), "#C96F4A", 10)
        for box in ((900, 920, 980, 1140), (960, 960, 1050, 1140), (930, 860, 1000, 1100)):
            ell(box, "#58B368")
    elif bg == "street":
        ground = 1230
        blds = [(-20, 270, 640, "#E5B8A8"), (240, 540, 430, "#A9B8D6"), (520, 780, 780, "#EBD79B"),
                (760, 1100, 560, "#B7D3B0")]
        for x0, x1, top, col in blds:
            rect((x0, top, x1, ground + 10), col)
            for wy in range(top + 60, ground - 80, 120):
                for wx in range(x0 + 40, x1 - 60, 90):
                    rect((wx, wy, wx + 46, wy + 70), "#FFF3C4" if (wx + wy) % 3 else "#8FB4D9")
        rect((-20, ground, 1100, 1570), "#C9C9D2")
        rect((-20, 1570, 1100, 1930), "#4A4F5C")
        for x in range(-40, 1100, 240):
            rect((x, 1720, x + 120, 1740), "#F2F2F2")
    elif bg == "home":
        rect((-20, 1250, 1100, 1930), "#B98B5E")
        rect((-20, 1230, 1100, 1260), "#8E6A47")
        rect((140, 360, 380, 600), "#8E6A47")
        rect((160, 380, 360, 580), "#9ED0C4")
        ell((200, 470, 320, 590), "#58B368")
        rect((120, 860, 720, 1230), "#7A8BE0", 60)
        rect((90, 1000, 220, 1230), "#5F70C9", 40)
        rect((620, 1000, 750, 1230), "#5F70C9", 40)
        rect((220, 1030, 620, 1230), "#8E9DEB", 30)
        rect((894, 880, 906, 1230), "#4A4F5C")
        rect((850, 1215, 950, 1240), "#4A4F5C", 10)
        poly([(830, 880), (970, 880), (940, 760), (860, 760)], "#F2C14E")
        ell((120, 1380, 960, 1600), "#E39A8C")


def _static_layer(bg, style, caption, sub, brand_key, size) -> Image.Image:
    brand = Brand(*brand_key)
    img = _bg_base(bg, style, brand, size)
    _draw_bg(img, bg, style, size)
    w, h = size
    u = w / 1080
    d = ImageDraw.Draw(img)
    chalk = style == "chalk"
    if caption:
        cap_col = _rgb(brand.accent) if not chalk else _rgb("#F4F4EF")
        for fs in range(66, 38, -4):
            font = load_font(max(8, int(fs * u)), True)
            lines = wrap_text(d, caption, font, int(w * 0.8))
            if len(lines) <= 3:
                break
        lines = lines[:3]
        lh = int(fs * 1.2 * u)
        tw = max(d.textlength(ln, font=font) for ln in lines)
        pad = int(30 * u)
        x0, y0 = (w - tw) / 2 - pad, int(120 * h / 1920)
        box = (x0, y0, x0 + tw + 2 * pad, y0 + lh * len(lines) + int(24 * u))
        if chalk:
            d.rounded_rectangle(box, int(24 * u), outline=cap_col, width=max(1, int(4 * u)))
            tcol = "#F4F4EF"
        else:
            d.rounded_rectangle(box, int(24 * u), fill=cap_col)
            tcol = "#FFFFFF" if _lum(cap_col) < 0.62 else "#1F2430"
        for i, ln in enumerate(lines):
            d.text((w / 2, y0 + int(12 * u) + i * lh), ln, font=font, fill=tcol, anchor="mt")
    if sub:
        for fs in range(46, 32, -2):
            font = load_font(max(8, int(fs * u)), True)
            lines = wrap_text(d, sub, font, int(w * 0.84))
            if len(lines) <= 2:
                break
        if len(lines) > 2:
            lines = lines[:2]
            while lines[1] and d.textlength(lines[1] + "…", font=font) > w * 0.84:
                lines[1] = lines[1][:-1].rstrip()
            lines[1] += "…"
        lh = int(fs * 1.25 * u)
        tw = max(d.textlength(ln, font=font) for ln in lines)
        pad = int(28 * u)
        bottom = int(h * 0.895)
        box = ((w - tw) / 2 - pad, bottom - lh * len(lines) - pad, (w + tw) / 2 + pad, bottom)
        d.rounded_rectangle(box, int(26 * u), fill="#0E0E14",
                            outline="#F4F4EF" if chalk else None, width=max(1, int(3 * u)))
        for i, ln in enumerate(lines):
            d.text((w / 2, box[1] + pad / 2 + i * lh), ln, font=font, fill="#FFFFFF", anchor="mt")
    if brand.name or brand.handle:
        font = load_font(max(8, int(26 * u)))
        y = h - int(h * 0.05) - int(30 * u)
        m = int(w * 0.085)
        px = img.getpixel((w // 2, min(h - 1, y)))
        col = "#1F2430" if _lum(px) > 0.55 else "#C8C8D0"
        if brand.name:
            d.text((m, y), brand.name, font=font, fill=col)
        if brand.handle:
            d.text((w - m, y), brand.handle, font=font, fill=col, anchor="ra")
    return img


@lru_cache(maxsize=24)
def _static_cached(bg, style, caption, sub, brand_key, size):
    return _static_layer(bg, style, caption, sub, brand_key, size)


def _bg_dynamic(img: Image.Image, style: str, brand: Brand, t: float):
    w, h = img.size
    d = ImageDraw.Draw(img)
    base = _rgb(CHALK_BG if style == "chalk" else brand.primary)
    for i in range(7):
        r = (0.06 + 0.035 * (i % 4)) * w
        x = w * ((0.12 + 0.14 * i + 0.04 * math.sin(t * 0.7 + i)) % 1.0)
        y = h * (0.18 + 0.1 * ((i * 3) % 7) + 0.03 * math.sin(t * 0.9 + i * 2))
        if style == "chalk":
            d.ellipse((x - r, y - r, x + r, y + r), outline="#F4F4EF", width=max(1, int(w / 270)))
        else:
            d.ellipse((x - r, y - r, x + r, y + r), fill=_mix(brand.accent, "#%02X%02X%02X" % base, 0.22))


# ───────────────────────── речевой пузырь ─────────────────────────

@lru_cache(maxsize=64)
def _bubble_layout(text, size):
    w, h = size
    u = w / 1080
    d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    pad = int(34 * u)
    max_text_w = int(w * 0.72) - 2 * pad
    max_h = int(h * 0.17)
    for fs in range(54, 30, -3):
        font = load_font(max(8, int(fs * u)), True)
        lines = wrap_text(d, text, font, max_text_w)
        lh = int(fs * 1.22 * u)
        if len(lines) * lh + 2 * pad <= max_h:
            break
    cap = max(1, (max_h - 2 * pad) // lh)
    if len(lines) > cap:
        lines = lines[:cap]
        while lines[-1] and d.textlength(lines[-1] + "…", font=font) > max_text_w:
            lines[-1] = lines[-1][:-1].rstrip()
        lines[-1] += "…"
    tw = max(d.textlength(ln, font=font) for ln in lines)
    return lines, fs, int(tw + 2 * pad), int(len(lines) * lh + 2 * pad), lh, pad


@lru_cache(maxsize=32)
def _bubble_img(text, size, style, tail_x, tail_h) -> Image.Image:
    lines, fs, bw, bh, lh, pad = _bubble_layout(text, size)
    u = size[0] / 1080
    chalk = style == "chalk"
    img = Image.new("RGBA", (bw + 4, bh + tail_h + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink, fill = ("#F4F4EF", CHALK_BG) if chalk else ("#1F2430", "#FFFFFF")
    ow = max(1, int(5 * u))
    d.rounded_rectangle((2, 2, 2 + bw, 2 + bh), int(40 * u), fill=fill, outline=ink, width=ow)
    tx = 2 + tail_x
    half = int(26 * u)
    d.polygon([(tx - half, 2 + bh - 2), (tx + half, 2 + bh - 2), (tx + int(10 * u), 2 + bh + tail_h)],
              fill=fill, outline=ink, width=ow)
    d.line([(tx - half + ow, 2 + bh - ow // 2), (tx + half - ow, 2 + bh - ow // 2)], fill=fill, width=ow + 2)
    font = load_font(max(8, int(fs * u)), True)
    for i, ln in enumerate(lines):
        d.text((2 + bw / 2, 2 + pad + i * lh), ln, font=font, fill=ink, anchor="mt")
    return img


# ───────────────────────── персонаж ─────────────────────────

_GEO = {  # ширина/высота тела, ширина/высота головы, длина ног, длина рук, разнос бёдер
    "round": dict(bw=250, bh=210, hw=240, hh=220, leg=120, arm=150, hip=58),
    "tall": dict(bw=150, bh=290, hw=180, hh=225, leg=130, arm=175, hip=36),
    "square": dict(bw=215, bh=225, hw=220, hh=205, leg=110, arm=155, hip=52),
}


def _pose(action: str, t: float, d_face: int, off: float, seed: float):
    """Позa: сдвиги и углы рук (0 — вниз, 90 — в сторону, 180 — вверх)."""
    p = dict(dx=0.0, body=4 * math.sin(2 * math.pi * 0.5 * t + seed), feet=0.0, hx=0.0, hy=0.0,
             al=12.0, ar=12.0, swing=0.0, talk=False, face=d_face)
    p["al"] += 3 * math.sin(2 * math.pi * 0.5 * t + seed)
    p["ar"] = p["al"]
    pointer = d_face or 1
    if action == "talk":
        p.update(talk=True, hy=3 * math.sin(2 * math.pi * 2 * t), hx=4 * math.sin(2 * math.pi * 1.1 * t + seed))
        g = 35 + 30 * (0.5 - 0.5 * math.cos(2 * math.pi * 1.4 * t))
        p["ar" if pointer > 0 else "al"] = g
    elif action == "walk":
        wt = t / WALK_T
        if wt < 1:
            ph = 2 * math.pi * 2.2 * t
            p.update(dx=off * (1 - _ease(wt)), swing=math.sin(ph), body=-abs(math.sin(ph)) * 10,
                     al=12 + 28 * math.sin(ph), ar=12 - 28 * math.sin(ph), face=(-1 if off > 0 else 1))
    elif action == "jump":
        ph = (t % 1.1) / 1.1
        h = 4 * ph * (1 - ph)
        p.update(body=-h * 150, feet=-h * 150, al=30 + 120 * h, ar=30 + 120 * h)
    elif action == "point":
        e = _ease(t / 0.4)
        a = 12 + (96 - 12) * e + 3 * math.sin(2 * math.pi * 3 * t) * e
        p["ar" if pointer > 0 else "al"] = a
        p["hx"] = pointer * 6 * e
    elif action == "shake":
        p.update(hx=9 * math.sin(2 * math.pi * 7 * t), dx=3 * math.sin(2 * math.pi * 9 * t),
                 al=140 + 8 * math.sin(2 * math.pi * 9 * t), ar=140 + 8 * math.sin(2 * math.pi * 9 * t + 1))
    elif action == "nod":
        p["hy"] = 16 * max(0.0, math.sin(2 * math.pi * t / 0.9))
    return p


def _draw_character(img, style, ch, cid, pos, emotion, action, t, cx, fy, k, seed_t):
    shape = _GEO[ch["shape"]]
    g = shape
    chalk = style == "chalk"
    pen = _Pen(img, style, k, seed=int(seed_t * 8) * 31 + hash(cid) % 97)
    d = pen.d
    d_face = {"left": 1, "right": -1}.get(pos, 0)
    seed = (hash(cid) % 628) / 100
    start = -(cx + 300 * k) if pos != "right" else (img.size[0] - cx + 300 * k)
    p = _pose(action, t, d_face, start, seed)
    cx = cx + p["dx"] + (3 * k * math.sin(seed_t * 50) if chalk else 0)
    col = ch["color"]
    ink = pen.ink
    pants = _shade(col, 0.55)
    skin = "#FFDDB8"
    X = lambda v: cx + v * k
    Y = lambda v: fy + v * k

    # тень на земле
    sh = 1 - min(0.5, -p["feet"] / 300)
    if not chalk:
        d.ellipse((X(-g["bw"] * 0.55 * sh), Y(-14), X(g["bw"] * 0.55 * sh), Y(14)), fill=_mix("#000000", "#C8C8C8", 0.18))
    else:
        d.line([(X(-g["bw"] * 0.6), Y(8)), (X(g["bw"] * 0.6), Y(8))], fill=pen.ink, width=max(1, int(3 * k)))

    # ноги
    body_dy = p["body"]
    hip_y = -g["leg"] + body_dy
    for side in (-1, 1):
        sw = p["swing"] * side
        fx = side * g["hip"] * 1.05 + sw * 38
        fyy = p["feet"] - max(0.0, sw) * 22
        pen.limb((X(side * g["hip"]), Y(hip_y)), (X(fx), Y(fyy - 10)), 34 * k, pants)
        sx_ = fx + (p["face"] or 0) * 14
        pen.shape("ell", (X(sx_ - 40), Y(fyy - 30), X(sx_ + 40), Y(fyy + 6)), "#2B3140")

    # тело
    btop = hip_y + 20 - g["bh"]
    bbox = (X(-g["bw"] / 2), Y(btop), X(g["bw"] / 2), Y(hip_y + 20))
    # задняя рука рисуем до тела, переднюю — после
    sh_y = btop + 48
    arms = []
    for side, ang in ((-1, p["al"]), (1, p["ar"])):
        a = math.radians(ang)
        sxp = side * (g["bw"] / 2 - 14)
        ex, ey = sxp + side * math.sin(a) * g["arm"], sh_y + math.cos(a) * g["arm"]
        arms.append(((X(sxp), Y(sh_y)), (X(ex), Y(ey)), side))
    sleeve = col
    for s, e, side in arms:
        pen.limb(s, e, 30 * k, sleeve)
    if ch["shape"] == "round":
        pen.shape("ell", bbox, col, double=True)
        d.ellipse((X(-g["bw"] * 0.22), Y(btop + g["bh"] * 0.38), X(g["bw"] * 0.22), Y(btop + g["bh"] * 0.88)),
                  fill=_mix("#FFFFFF", pen.fill(col), 0.28) if not chalk else None)
    else:
        pen.shape("rect", bbox, col, r=(55 if ch["shape"] == "tall" else 36) * k, double=True)
        if ch["shape"] == "tall":
            pen.poly([(X(-14), Y(btop + 6)), (X(14), Y(btop + 6)), (X(22), Y(btop + 120)), (X(0), Y(btop + 150)),
                      (X(-22), Y(btop + 120))], "#D9534F" if not chalk else col)
        else:
            for i in range(3):
                yy = btop + 60 + i * 55
                d.ellipse((X(-7), Y(yy - 7), X(7), Y(yy + 7)), fill=ink)
    for s, e, side in arms:
        r = 19 * k
        pen.shape("ell", (e[0] - r, e[1] - r, e[0] + r, e[1] + r), skin, double=False)

    # голова
    hx = p["hx"] + (d_face * 8 if action != "shake" else 0)
    hcy = btop - g["hh"] / 2 + 28 + p["hy"] + (0 if action != "jump" else 0)
    hbox = (X(hx - g["hw"] / 2), Y(hcy - g["hh"] / 2), X(hx + g["hw"] / 2), Y(hcy + g["hh"] / 2))
    hair = ink if chalk else _HAIR[hash(cid) % len(_HAIR)]
    if ch["shape"] == "square":
        pen.shape("rect", hbox, skin, r=52 * k, double=True)
        d.rounded_rectangle((hbox[0] + pen.ow, hbox[1] - 6 * k, hbox[2] - pen.ow, Y(hcy - g["hh"] * 0.28)),
                            int(36 * k), fill=hair)
    else:
        pen.shape("ell", hbox, skin, double=True)
        d.pieslice((hbox[0] + 1, hbox[1] + 1, hbox[2] - 1, hbox[3] - 1), 188, 352, fill=hair)
        if ch["shape"] == "tall":
            d.ellipse((X(hx - 22), Y(hcy - g["hh"] / 2 - 26), X(hx + 26), Y(hcy - g["hh"] / 2 + 14)), fill=hair)
    _face(pen, img, emotion, p, X, Y, hx, hcy, g, k, t, seed)
    return Y(hcy - g["hh"] / 2)  # верх головы, y в пикселях


def _face(pen, img, emotion, p, X, Y, hx, hcy, g, k, t, seed):
    d = pen.d
    chalk = pen.chalk
    ink = pen.ink
    f = p["face"] * 8
    ey = hcy + g["hh"] * 0.04
    dx = g["hw"] * 0.21
    er = g["hw"] * (0.15 if emotion == "surprised" else 0.125)
    pr = er * (0.38 if emotion == "surprised" else 0.5)
    white = pen.bg if chalk else "#FFFFFF"
    pup = "#F4F4EF" if chalk else "#1F2430"
    look = (p["face"] * 8, 0.0)
    if emotion == "thinking":
        look = (p["face"] * 9 + 6, -er * 0.45)
    blink = ((t + seed) % 3.4) < 0.12
    lid = {"angry": 0.32, "sad": 0.25}.get(emotion, 0.0)
    for side in (-1, 1):
        ex = hx + f + side * dx
        if blink:
            pen.line((X(ex - er), Y(ey)), (X(ex + er), Y(ey)), 6 * k)
            continue
        d.ellipse((X(ex - er), Y(ey - er), X(ex + er), Y(ey + er)), fill=white, outline=ink, width=max(1, pen.ow // 2 + 1))
        px_, py_ = ex + look[0] * 0.5, ey + look[1]
        d.ellipse((X(px_ - pr), Y(py_ - pr), X(px_ + pr), Y(py_ + pr)), fill=pup)
        if lid:  # тяжёлое веко: срезаем верх глаза цветом головы
            skin = pen.fill("#FFDDB8")
            lift = er * (2 * lid)
            if emotion == "angry":
                pts = [(ex - side * er * 1.2 + side * er * 2.4, ey - er * 1.3), (ex - side * er * 1.2, ey - er * 1.3),
                       (ex - side * er * 1.2, ey - er + lift * 0.2)]
                inner = ex - side * er
                d.polygon([(X(ex - er * 1.1), Y(ey - er * 1.1)), (X(ex + er * 1.1), Y(ey - er * 1.1)),
                           (X(ex + side * -er * 1.1 if side < 0 else ex - er * 1.1), Y(ey - er * 0.2 + 0))][:0] or
                          [(0, 0), (0, 0), (0, 0)], fill=skin) if False else None
            else:
                pass
    # брови
    by = ey - er * 1.9
    spec = {"neutral": (-2, -2), "happy": (-6, -12), "angry": (10, -10), "sad": (-14, 6),
            "surprised": (-18, -18), "thinking": (-2, -2)}[emotion]
    for side in (-1, 1):
        raise_ = 14 if (emotion == "thinking" and side == 1) else 0
        ox = hx + f + side * (dx + er * 0.95)
        ix = hx + f + side * (dx - er * 0.95)
        pen.line((X(ox), Y(by + spec[1] - raise_)), (X(ix), Y(by + spec[0] - raise_)), 9 * k)
    # рот
    my = hcy + g["hh"] * 0.28
    mx = hx + f * 0.8
    mouth_fill = pen.bg if chalk else "#8C2F39"
    if p["talk"]:
        o = 0.5 + 0.5 * math.sin(2 * math.pi * 4.2 * t + seed)
        o = max(o, 0.5 + 0.5 * math.sin(2 * math.pi * 6.1 * t))
        wmod = {"surprised": 30, "happy": 50, "angry": 44, "sad": 36}.get(emotion, 40)
        hh_ = 8 + 38 * o
        box = (X(mx - wmod), Y(my - hh_ * 0.4), X(mx + wmod), Y(my + hh_))
        d.ellipse(box, fill=mouth_fill, outline=ink, width=max(1, pen.ow // 2 + 1))
        if hh_ > 26 and not chalk:
            d.ellipse((X(mx - wmod * 0.5), Y(my + hh_ * 0.45), X(mx + wmod * 0.5), Y(my + hh_ * 0.95)), fill="#E8707E")
    elif emotion == "happy":
        d.chord((X(mx - 52), Y(my - 38), X(mx + 52), Y(my + 38)), 0, 180, fill=mouth_fill, outline=ink,
                width=max(1, pen.ow // 2 + 1))
    elif emotion == "sad":
        d.arc((X(mx - 38), Y(my - 4), X(mx + 38), Y(my + 46)), 200, 340, fill=ink, width=max(2, int(8 * k)))
    elif emotion == "angry":
        pen.line((X(mx - 36), Y(my + 8)), (X(mx + 36), Y(my + 2)), 9 * k)
    elif emotion == "surprised":
        d.ellipse((X(mx - 20), Y(my - 12), X(mx + 20), Y(my + 36)), fill=mouth_fill, outline=ink,
                  width=max(1, pen.ow // 2 + 1))
    elif emotion == "thinking":
        pen.line((X(mx - 6), Y(my + 8)), (X(mx + 34), Y(my - 4)), 8 * k)
    else:
        pen.line((X(mx - 30), Y(my + 4)), (X(mx + 30), Y(my + 4)), 8 * k)
    if emotion == "happy" and not chalk:
        for side in (-1, 1):
            cxp = hx + f + side * g["hw"] * 0.33
            d.ellipse((X(cxp - 20), Y(hcy + g["hh"] * 0.17 - 12), X(cxp + 20), Y(hcy + g["hh"] * 0.17 + 12)),
                      fill="#F8A5A5")


# ───────────────────────── кадр ─────────────────────────

def render_frame(script: dict, scene_index: int, t: float, brand: Brand, size=(1080, 1920)) -> Image.Image:
    if not script.get("_normalized"):
        script = normalize_script(script)
    sc = script["scenes"][scene_index]
    style = script["style"]
    w, h = size
    k = min(w / 1080, h / 1920) * CS
    line = sc.get("line")
    sub = line["text"] if line else ""
    bkey = (brand.name, brand.handle, brand.primary, brand.accent, brand.text)
    img = _static_cached(sc["bg"], style, sc.get("caption") or "", sub, bkey, (w, h)).copy()
    if sc["bg"] == "abstract":
        # динамические круги рисуем поверх статичного градиента, но под подписями нельзя — поэтому до персонажей
        _bg_dynamic(img, style, brand, t)
        # подписи и бренд перерисованы поверх кругов
        top = _static_cached("none", style, sc.get("caption") or "", sub, bkey, (w, h))
        if style == "flat":
            pass
        del top

    fy = h * FEET_Y
    xs = {"left": 0.27, "center": 0.5, "right": 0.73}
    tops, speaker_xy, walk_end = {}, None, 0.0
    order = sorted(sc["characters"], key=lambda c: POSITIONS.index(c["pos"]))
    for c in order:
        cid = c["id"]
        cx = w * xs[c["pos"]]
        top = _draw_character(img, style, script["characters"][cid], cid, c["pos"], c["emotion"], c["action"],
                              t, cx, fy, k, t)
        tops[cid] = (cx, top)
        # подпись с именем под ногами
        font = load_font(max(8, int(30 * min(w / 1080, h / 1920))), True)
        d = ImageDraw.Draw(img)
        name = script["characters"][cid]["name"]
        tw = d.textlength(name, font=font)
        pad = 14 * w / 1080
        ty = fy + 28 * w / 1080
        d.rounded_rectangle((cx - tw / 2 - pad, ty, cx + tw / 2 + pad, ty + 50 * w / 1080), int(22 * w / 1080),
                            fill="#0E0E14")
        d.text((cx, ty + 25 * w / 1080), name, font=font, fill="#FFFFFF", anchor="mm")
        if line and line["speaker"] == cid:
            speaker_xy = tops[cid]
            walk_end = WALK_T if c["action"] == "walk" else 0.0

    if line and speaker_xy and t >= max(0.15, walk_end):
        scale = 0.6 + 0.4 * _ease((t - max(0.15, walk_end)) / 0.25)
        _, _, bw, bh, _, _ = _bubble_layout(line["text"], (w, h))
        cx, top = speaker_xy
        margin = int(w * 0.05)
        bx = min(max(cx - bw / 2, margin), w - margin - bw)
        tail_h = int(h * 0.026)
        tail_x = int(min(max(cx - bx, bw * 0.15), bw * 0.85))
        by = max(int(h * 0.2), top - 8 - tail_h - bh)
        tail_h = max(int(h * 0.01), int(top - 8 - by - bh))
        bimg = _bubble_img(line["text"], (w, h), style, tail_x, tail_h)
        ax, ay = bx + tail_x + 2, by + bh + tail_h + 2  # якорь — кончик хвоста
        if scale < 1:
            bimg = bimg.resize((max(1, int(bimg.width * scale)), max(1, int(bimg.height * scale))), Image.BILINEAR)
            ox, oy = int((tail_x + 2) * scale), int((bh + tail_h + 2) * scale)
        else:
            ox, oy = tail_x + 2, bh + tail_h + 2
        img.paste(bimg, (int(ax - ox), int(ay - oy)), bimg)

    if t < TRANSITION:
        base = Image.new("RGB", img.size, CHALK_BG if style == "chalk" else brand.primary)
        img = Image.blend(base, img, _ease(t / TRANSITION))
    return img


# ───────────────────────── сборка видео ─────────────────────────

def _render_chunk(script, si, frames, fps, brand, size) -> bytes:
    return b"".join(render_frame(script, si, f / fps, brand, size).tobytes() for f in frames)


async def build_toon(script: dict, brand: Brand, out_path: str, *, size=(1080, 1920), fps: int = 24,
                     render_scale: float = 0.5, music_path: str | None = None) -> ToonResult:
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg не установлен")
    script = normalize_script(script)
    out_w, out_h = size[0] // 2 * 2, size[1] // 2 * 2
    rw, rh = max(2, int(size[0] * render_scale) // 2 * 2), max(2, int(size[1] * render_scale) // 2 * 2)
    tmp = tempfile.mkdtemp(prefix="lhtoon_")
    proc = None
    try:
        counts, audio_in = [], []
        for i, sc in enumerate(script["scenes"]):
            dur = sc["duration"]
            if sc["audio"]:
                ap = os.path.join(tmp, f"a{i}.audio")
                with open(ap, "wb") as f:
                    f.write(sc["audio"])
                pd = await probe_duration(ap)
                if pd > 0:
                    dur, audio_in = pd + PAD, audio_in
                else:
                    ap = None
            else:
                ap = None
            counts.append(max(1, round(dur * fps)))
            audio_in.append(ap)
        durs = [n / fps for n in counts]
        total = sum(durs)

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{rw}x{rh}", "-r", str(fps), "-i", "-"]
        filt, n = [f"[0:v]scale={out_w}:{out_h}:flags=lanczos,format=yuv420p[vout]"], 1
        norm = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
        for i, (ap, d) in enumerate(zip(audio_in, durs)):
            if ap:
                cmd += ["-i", ap]
            else:
                cmd += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
            filt.append(f"[{n}:a]{norm},apad=whole_dur={d:.3f},atrim=0:{d:.3f},asetpts=N/SR/TB[s{i}]")
            n += 1
        filt.append("".join(f"[s{i}]" for i in range(len(durs))) + f"concat=n={len(durs)}:v=0:a=1[voice]")
        if music_path:
            cmd += ["-stream_loop", "-1", "-i", music_path]
            filt.append(f"[{n}:a]{norm},volume=0.15[mus]")
            # amix делит сумму на число входов, поэтому голос добираем volume=2
            filt.append("[voice][mus]amix=inputs=2:duration=first:dropout_transition=0,volume=2,alimiter=limit=0.95[aout]")
        else:
            filt.append("[voice]anull[aout]")
        cmd += ["-filter_complex", ";".join(filt), "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-r", str(fps), "-t", f"{total:.3f}", "-movflags", "+faststart", out_path]

        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        err_task = asyncio.create_task(proc.stderr.read())
        chunk = max(1, fps)
        try:
            for si, cnt in enumerate(counts):
                for s in range(0, cnt, chunk):
                    data = await asyncio.to_thread(_render_chunk, script, si, range(s, min(cnt, s + chunk)),
                                                   fps, brand, (rw, rh))
                    proc.stdin.write(data)
                    await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        code = await proc.wait()
        err = (await err_task).decode(errors="replace")
        if code != 0:
            raise RuntimeError(f"ffmpeg завершился с кодом {code}: {err[-800:]}")
        return ToonResult(path=out_path, duration=await probe_duration(out_path), scenes=len(counts))
    finally:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
