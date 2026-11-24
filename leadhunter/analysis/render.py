"""Досье текстом для Telegram (HTML, до 4096 символов)."""
import html

LIMIT = 4000
FEATURES = (("https", "HTTPS"), ("mobile", "мобильная версия"), ("robots", "robots.txt"), ("sitemap", "карта сайта"),
            ("schema", "микроразметка"), ("forms", "форма заявки"))
_e = html.escape


def _stars(rating, reviews) -> str:
    if not rating:
        return ""
    return f"★{float(rating):.1f}" + (f" ({int(reviews)})" if reviews else "")


def site_lines(site: dict, from_text: bool) -> list[str]:
    head = f"🌐 <b>Сайт</b> {_e(site.get('domain', ''))}" + (" — ссылка из текста заказа" if from_text else "")
    if not site.get("ok"):
        return [head, f"⚠️ {_e(site.get('error') or 'не удалось проверить')}"]
    lines = [head]
    if site.get("problems"):
        lines.append("⚠️ " + _e("; ".join(site["problems"])))
    else:
        lines.append("Явных проблем не найдено.")
    if site.get("title"):
        lines.append(f"Заголовок: «{_e(site['title'][:120])}»")
    facts = [f"ответ {site['seconds']:.1f} с", f"{site['kb']} КБ"]
    if site.get("cms"):
        facts.insert(0, f"сделан на {_e(site['cms'])}")
    if site.get("year"):
        facts.append(f"в подвале {site['year']} год")
    lines.append(" · ".join(facts))
    for label, wanted in (("Есть", True), ("Нет", False)):
        names = [name for key, name in FEATURES if bool(site.get(key)) is wanted]
        if names:
            lines.append(f"{label}: " + ", ".join(names))
    return lines


def geo_line(geo: dict) -> str:
    """Адрес с карт обычно уже начинается с города — второй раз его не пишем."""
    city, address = str(geo.get("city") or ""), str(geo.get("address") or "")
    place = address if city and city.lower() in address.lower() else ", ".join(x for x in (city, address) if x)
    stars = _stars(geo.get("rating"), geo.get("reviews"))
    return place + (f" · {stars}" if stars else "")


def render(lead_id: int, d: dict, limit: int = LIMIT) -> str:
    head = f"🔎 <b>Досье на лид #{lead_id}</b>" + (f" · {_e(d['title'][:80])}" if d.get("title") else "")
    blocks: list[tuple[str, list[str]]] = []  # (ключ, строки); при нехватке места блоки убираются с конца списка DROP
    if d.get("site"):
        blocks.append(("site", site_lines(d["site"], d.get("site_from_text", False))))
    if seo := d.get("seo"):
        where = f"на {seo['position']} месте" if seo.get("position") else f"в первых {seo['depth']} результатах не найден"
        blocks.append(("seo", [f"🔍 <b>Поиск</b>: по запросу «{_e(seo['query'])}» сайт {where} ({_e(seo['engine'])})"]))
    if market := d.get("market"):
        lines = [f"🏁 <b>Конкуренты</b>: из {market['total']} компаний по запросу «{_e(market['query'])}» "
                 f"сайт есть у {market['with_site']}"]
        for c in d.get("competitors") or []:
            rep = c.get("report") or {}
            state = (_e(rep.get("error") or "не открывается") if not rep.get("ok")
                     else _e("; ".join(rep["problems"][:3])) if rep.get("problems") else "проблем не найдено")
            stars = _stars(c.get("rating"), c.get("reviews"))
            lines.append(f"• {_e(c['title'][:50])} — {_e(rep.get('domain') or c['site'])}" + (f" · {stars}" if stars else "")
                         + f" · {state}")
        blocks.append(("competitors", lines))
    if geo := d.get("geo"):
        blocks.append(("geo", ["📍 <b>Гео</b>: " + _e(geo_line(geo))]))
    if ai := d.get("ai"):
        lines = ["🤖 <b>Вывод</b>"]
        for label, key in (("Что болит", "pain"), ("Что предложить", "offer"), ("Цена", "price")):
            if ai.get(key):
                lines.append(f"{label}: {_e(ai[key])}")
        if ai.get("questions"):
            lines.append("Что уточнить: " + _e("; ".join(ai["questions"])))
        blocks.append(("ai", lines))
        if ai.get("message"):
            blocks.append(("message", ["✍️ <b>Первое сообщение</b>", f"<code>{_e(ai['message'])}</code>"]))
    if d.get("notes"):
        blocks.append(("notes", ["ℹ️ " + _e(n) for n in d["notes"]]))

    def join(items) -> str:
        return "\n\n".join([head] + ["\n".join(lines) for _, lines in items])

    for drop in ("", "geo", "competitors", "notes", "seo", "site", "ai"):
        blocks = [b for b in blocks if b[0] != drop]
        if len(join(blocks)) <= limit:
            break
    return join(blocks)
