"""Публичная страница лид-магнита: содержание + форма «получить». Заявка уходит в пайплайн как горячий лид."""
import html

from ..content.render import Brand, _rgb, valid_color


def _color(brand, key: str) -> str:
    """Цвет попадает в CSS страницы: всё, что не проходит общее правило цвета, заменяем цветом по умолчанию."""
    value = getattr(brand, key, "")
    return value.strip() if valid_color(value) else Brand.__dataclass_fields__[key].default


def _on(color: str) -> str:
    """Цвет надписи на кнопке: на светлом акценте (белый по умолчанию) белый текст не виден."""
    r, g, b = _rgb(color)
    return "#fff" if 0.299 * r + 0.587 * g + 0.114 * b < 150 else "#000"


def _page(title: str, brand, body: str) -> str:
    e = html.escape
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title>
<link rel="icon" href="data:,">
<style>
:root{{--bg:{_color(brand, "primary")};--ac:{_color(brand, "accent")};--fg:{_color(brand, "text")};--on:{_on(_color(brand, "accent"))}}}
*{{box-sizing:border-box}}body{{margin:0;font:17px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:640px;margin:0 auto;padding:40px 20px 80px}}h1{{font-size:34px;line-height:1.15;margin:0 0 12px}}
.sub{{opacity:.8;font-size:19px;margin-bottom:32px}}.bar{{width:64px;height:6px;background:var(--ac);border-radius:3px;margin-bottom:24px}}
ol{{padding-left:0;list-style:none;counter-reset:n}}li{{counter-increment:n;margin:0 0 18px;padding-left:46px;position:relative}}
li::before{{content:counter(n);position:absolute;left:0;top:2px;width:32px;height:32px;border-radius:50%;background:var(--ac);color:var(--on);font-weight:700;display:flex;align-items:center;justify-content:center;font-size:15px}}
li b{{display:block;margin-bottom:2px}}li span{{opacity:.85}}
form{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:22px;margin-top:36px}}
label{{display:block;font-size:14px;opacity:.8;margin:12px 0 4px}}input,textarea{{width:100%;padding:12px;border-radius:10px;border:1px solid rgba(255,255,255,.2);background:rgba(0,0,0,.25);color:var(--fg);font:inherit}}
button{{margin-top:18px;width:100%;padding:14px;border:0;border-radius:12px;background:var(--ac);color:var(--on);font:600 17px inherit;cursor:pointer}}
.hp{{position:absolute;left:-9999px}}.err{{border:1px solid var(--ac);border-radius:10px;padding:10px 12px;margin:0 0 12px;font-size:15px}}footer{{opacity:.5;font-size:13px;margin-top:40px;text-align:center}}
</style></head><body><main>{body}
<footer>{e(brand.name)} {e(brand.handle)}</footer></main></body></html>"""


def render_landing(row, data: dict, brand, token: str = "", values: dict | None = None, error: str = "") -> str:
    """token — одноразовый токен формы; values и error — чтобы вернуть человеку форму с его текстом и подсказкой."""
    e = html.escape
    v = values or {}
    title = data.get("title") or row["title"] or row["topic"]
    items = "".join(f"<li><b>{e(i.get('title', ''))}</b><span>{e(i.get('text', ''))}</span></li>" for i in data.get("items") or [])
    cta = data.get("cta") or "Оставьте контакт — пришлём материал и разберём вашу задачу."
    body = f"""<div class="bar"></div><h1>{e(title)}</h1><p class="sub">{e(data.get('subtitle', ''))}</p>
<ol>{items}</ol>
<form method="post">{f'<p class="err">{e(error)}</p>' if error else ''}<p style="margin:0 0 6px;font-weight:600">{e(cta)}</p>
<label>Как вас зовут</label><input name="name" maxlength="100" value="{e(v.get('name', ''))}">
<label>Телефон или Telegram</label><input name="contact" required maxlength="100" placeholder="@username или +7…" value="{e(v.get('contact', ''))}">
<label>Что хотите сделать (необязательно)</label><textarea name="message" rows="3" maxlength="1000">{e(v.get('message', ''))}</textarea>
<input type="hidden" name="token" value="{e(token)}">
<input class="hp" name="website" tabindex="-1" autocomplete="off">
<button type="submit">Получить</button></form>"""
    return _page(title, brand, body)


def render_thanks(row, brand) -> str:
    e = html.escape
    body = (f'<div class="bar"></div><h1>Спасибо!</h1><p class="sub">Заявка по «{e(row["title"] or row["topic"])}» получена — '
            f'напишем вам в ближайшее время.</p>')
    return _page("Спасибо", brand, body)


def render_notice(title: str, text: str, brand) -> str:
    e = html.escape
    return _page(title, brand, f'<div class="bar"></div><h1>{e(title)}</h1><p class="sub">{e(text)}</p>')
