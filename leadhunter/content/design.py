"""Слайды, обложки и сцены видео как HTML-страницы в стиле сайта владельца: чёрный фон, крупный Unbounded,
обведённые слова-акценты, мягкое свечение и зерно. Страницы снимает браузер (browser_render);
без браузера те же тексты рисует Pillow (render) — проще, но теми же шрифтами."""
import html
import os
import re
from pathlib import Path

from .render import MARK, Brand, _mix, _rgb, plain

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_FILES = {"Unbounded": "Unbounded.ttf", "Manrope": "Manrope.ttf", "Mono": "JetBrainsMono.ttf"}
SLIDE_SIZE = (1080, 1350)
SCENE_SIZE = (1080, 1920)
TITLE_LIMIT, TEXT_LIMIT = 140, 420
MOTION_SECONDS = 4.0  # столько длится анимация одной страницы; дальше кадр стоит
# эмодзи и значки: шрифты слайдов их не содержат, на картинке были бы пустые квадраты
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF"
                   "\u200d\ufe0f\u20e3\U000E0020-\U000E007F]+")


def _cut(text, limit: int) -> str:
    text = re.sub(r"[ \t]+", " ", EMOJI.sub("", str(text or ""))).strip()
    if len(plain(text)) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-*") + "…"


def _words(text: str, start: int = 0) -> tuple[str, int]:
    """Слова в отдельных блоках (в анимации выезжают по одному), *акцент* — обведённым контуром."""
    out, i = [], start
    for line in text.split("\n"):
        parts = []
        for chunk in re.split(r"(\*[^*\n]{1,60}\*)", line):
            accent = bool(MARK.fullmatch(chunk))
            for word in chunk.strip("*").split() if accent else chunk.replace("*", "").split():
                word = html.escape(word)
                parts.append(f'<span class="w" style="--i:{i}">{f"<em>{word}</em>" if accent else word}</span>')
                i += 1
        if parts:
            out.append(" ".join(parts))
    return "<br>".join(out), i


def _text(text: str) -> str:
    return "<br>".join(html.escape(line) for line in plain(text).split("\n") if line.strip())


def _dark(color: str) -> bool:
    r, g, b = _rgb(color)
    return 0.299 * r + 0.587 * g + 0.114 * b < 140


def _hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(rgb)


def _css(brand: Brand, size, motion: bool) -> str:
    w, h = size
    faces = "".join(
        f"@font-face{{font-family:'{name}';src:url('{Path(FONTS_DIR, file).as_uri()}');font-weight:100 900}}"
        for name, file in FONT_FILES.items())
    ink = ",".join(str(c) for c in _rgb(brand.text))
    dark = _dark(brand.primary)
    css = f"""{faces}
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{width:{w}px;height:{h}px;overflow:hidden}}
body{{--k:1;--bg:{brand.primary};--ink:{brand.text};--acc:{brand.accent};--mute:{_hex(_mix(brand.text, brand.primary, .58))};
--card:{_hex(_mix(brand.text, brand.primary, .08))};background:var(--bg);color:var(--ink);font-family:'Manrope',sans-serif;position:relative}}
.glow{{position:absolute;inset:-12%;background:radial-gradient(700px 520px at 82% 14%,rgba({ink},.17),transparent 70%),
radial-gradient(640px 640px at 8% 92%,rgba({ink},.09),transparent 70%)}}
.grain{{position:absolute;inset:0;opacity:{'.10' if dark else '.20'};mix-blend-mode:{'screen' if dark else 'multiply'}}}
.wrap{{position:absolute;inset:0;padding:{round(h * .071)}px 92px {round(h * .062)}px;display:flex;flex-direction:column}}
.top,.bot{{display:flex;justify-content:space-between;align-items:center;gap:40px;font-family:'Mono',monospace;font-size:25px;
letter-spacing:3px;text-transform:uppercase;color:var(--mute);white-space:nowrap}}
.top span,.bot span{{overflow:hidden;text-overflow:ellipsis}}
.bot b{{color:var(--ink);font-weight:500;text-transform:none;letter-spacing:2px}}
.mid{{flex:1;min-height:0;display:flex;flex-direction:column;justify-content:center;overflow:hidden;padding:24px 0}}
.in{{display:flex;flex-direction:column;gap:calc(44px*var(--k))}}
h1,h2{{font-family:'Unbounded',sans-serif;font-weight:800;line-height:1.08;letter-spacing:-2px}}
h1{{font-size:calc(104px*var(--k))}}h2{{font-size:calc(78px*var(--k))}}
.scene h1{{font-size:calc(112px*var(--k))}}
.w{{display:inline-block}}
em{{font-style:normal;color:transparent;-webkit-text-stroke:calc(2.5px*var(--k)) var(--acc)}}
p{{font-size:calc(42px*var(--k));line-height:1.42;font-weight:500;color:var(--ink);opacity:.86}}
p.sub{{color:var(--mute);opacity:1;font-size:calc(40px*var(--k))}}
.rule{{width:120px;height:4px;background:var(--acc)}}
.num{{font-family:'Unbounded',sans-serif;font-size:calc(250px*var(--k));line-height:.82;font-weight:800;color:transparent;
-webkit-text-stroke:3px var(--acc);letter-spacing:-8px}}
.pill{{align-self:flex-start;padding:30px 46px;border-radius:999px;background:var(--ink);color:var(--bg);
font-size:calc(38px*var(--k));font-weight:700;white-space:nowrap}}
.win{{border-radius:26px;overflow:hidden;background:var(--card);border:1px solid rgba({ink},.22);box-shadow:0 40px 90px rgba(0,0,0,.35)}}
.win .bar{{height:52px;display:flex;align-items:center;gap:12px;padding:0 22px;background:rgba({ink},.10)}}
.win .bar i{{width:15px;height:15px;border-radius:50%;background:rgba({ink},.35)}}
.win img{{display:block;width:100%;height:calc(520px*var(--k));object-fit:cover;object-position:top}}
.shot h2{{font-size:calc(64px*var(--k))}}.shot p{{font-size:calc(36px*var(--k))}}
.tight h1,.tight h2,.tight p{{overflow-wrap:anywhere}}
"""
    if motion:
        css += """@keyframes up{from{opacity:0;transform:translateY(70px)}to{opacity:1;transform:none}}
@keyframes grow{from{width:0}to{width:120px}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes drift{from{transform:translate(0,0) scale(1)}to{transform:translate(-80px,60px) scale(1.22)}}
.glow{animation:drift var(--dur,4s) ease-in-out both}
.rule{animation:grow .7s cubic-bezier(.2,.8,.2,1) .1s both}
.w{animation:up .75s cubic-bezier(.2,.8,.2,1) calc(.25s + var(--i)*.07s) both}
.num,.win{animation:up .8s cubic-bezier(.2,.8,.2,1) .15s both}
p,.pill{animation:up .8s cubic-bezier(.2,.8,.2,1) calc(.4s + var(--n)*.07s) both}
.top,.bot{animation:fade .6s ease calc(.5s + var(--n)*.07s) both}
"""
    return css


GRAIN = ('<svg class="grain" xmlns="http://www.w3.org/2000/svg" width="100%" height="100%"><filter id="n">'
         '<feTurbulence type="fractalNoise" baseFrequency=".85" numOctaves="2" stitchTiles="stitch"/>'
         '<feColorMatrix values="0 0 0 0 .5  0 0 0 0 .5  0 0 0 0 .5  0 0 0 .55 0"/></filter>'
         '<rect width="100%" height="100%" filter="url(#n)"/></svg>')


def _page(inner: str, brand: Brand, size, *, counter: str = "", corner: str = "", cls: str = "", words: int = 0,
          motion: bool = False, seconds: float = MOTION_SECONDS) -> str:
    e = html.escape
    label = e(_cut(brand.name, 28))
    handle = e(_cut(brand.handle, 28))
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>{_css(brand, size, motion)}</style></head>'
            f'<body class="{cls}" style="--n:{words};--dur:{seconds:.2f}s"><div class="glow"></div>{GRAIN}<div class="wrap">'
            f'<div class="top"><span>{label}</span><span>{e(counter)}</span></div>'
            f'<div class="mid"><div class="in">{inner}</div></div>'
            f'<div class="bot"><b>{handle}</b><span>{e(corner)}</span></div></div></body></html>')


def slide_html(title: str, text: str, index: int, total: int, brand: Brand, *, size=SLIDE_SIZE, image: str = "",
               number: int | None = None, motion: bool = False) -> str:
    """Один слайд. Первый — обложка, последний — призыв с кнопкой, остальные — пункт с номером;
    image — путь к снимку сайта: слайд с окном браузера; number — номер пункта, если он не совпадает с порядком слайда."""
    title, text = _cut(title, TITLE_LIMIT), _cut(text, TEXT_LIMIT)
    last = index == total
    counter = f"{index:02d} / {total:02d}" if total > 1 else ""
    corner = _cut(brand.site, 32) if last else "листайте →"
    if index == 1:
        head, n = _words(title)
        inner = f'<div class="rule"></div><h1>{head}</h1>' + (f'<p class="sub">{_text(text)}</p>' if text else "")
        cls = "cover"
    elif image:
        head, n = _words(title)
        inner = (f'<h2>{head}</h2><div class="win"><div class="bar"><i></i><i></i><i></i></div>'
                 f'<img src="{html.escape(Path(image).as_uri())}"></div>' + (f"<p>{_text(text)}</p>" if text else ""))
        cls = "shot"
    elif last and total > 2:
        head, n = _words(title)
        inner = f'<div class="rule"></div><h2>{head}</h2>' + (f"<p>{_text(text)}</p>" if text else "")
        if brand.handle:
            inner += f'<div class="pill">{html.escape(_cut(brand.handle, 28))} →</div>'
        cls = "cta"
    else:
        head, n = _words(title)
        inner = (f'<div class="num">{index - 1 if number is None else number:02d}</div>' if total > 2 else '<div class="rule"></div>')
        inner += (f"<h2>{head}</h2>" if title else "") + (f"<p>{_text(text)}</p>" if text else "")
        cls = "point"
    return _page(inner, brand, size, counter=counter, corner=corner, cls=cls, words=n, motion=motion)


def carousel_html(slides: list, brand: Brand, *, images: dict | None = None, size=SLIDE_SIZE) -> list[str]:
    """images — {номер слайда с нуля: путь к снимку сайта}."""
    slides = [s if isinstance(s, dict) else {"title": "", "text": s} for s in slides or []]
    images, pages, number = images or {}, [], 0
    for i, s in enumerate(slides, 1):
        image = images.get(i - 1, "")
        if 1 < i and not image and not (i == len(slides) and len(slides) > 2):
            number += 1  # номера идут подряд по пунктам: обложка, слайд со снимком и призыв их не занимают
        pages.append(slide_html(str(s.get("title") or ""), str(s.get("text") or ""), i, len(slides), brand, size=size,
                                image=image, number=number))
    return pages


def cover_html(title: str, subtitle: str, brand: Brand, *, size=SLIDE_SIZE, motion: bool = False) -> str:
    return slide_html(title, subtitle, 1, 1, brand, size=size, motion=motion)


def scene_html(text: str, brand: Brand, *, index: int = 1, total: int = 1, subtitle: str = "", size=SCENE_SIZE,
               motion: bool = True, seconds: float = MOTION_SECONDS) -> str:
    """Сцена вертикального видео: крупная фраза, слова выезжают по одному."""
    head, n = _words(_cut(text, TITLE_LIMIT))
    sub = _cut(subtitle, 160)
    inner = f'<div class="rule"></div><h1>{head}</h1>' + (f'<p class="sub">{_text(sub)}</p>' if sub else "")
    counter = f"{index:02d} / {total:02d}" if total > 1 else ""
    return _page(inner, brand, size, counter=counter, corner=_cut(brand.site, 32), cls="scene", words=n, motion=motion,
                 seconds=min(seconds, MOTION_SECONDS))
