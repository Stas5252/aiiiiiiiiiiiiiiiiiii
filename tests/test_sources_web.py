import json

from leadhunter.sources.freelance_ru import parse_freelance_ru_html
from leadhunter.sources.instagram_web import parse_tag_info
from leadhunter.sources.kwork import parse_kwork_html
from leadhunter.sources.threads_web import parse_threads_html
from leadhunter.sources.weblancer import parse_weblancer_html
from leadhunter.sources.yandex_maps_web import audit_lead, business_lead, clean_site, parse_yandex_items


def test_parse_threads_html():
    post = {"pk": "4016990784607610790", "code": "De_N9Xujeem", "taken_at": 1793082639,
            "user": {"username": "dexoni.brand"}, "caption": {"text": "Нужен сайт для магазина одежды"}}
    reply = {"pk": "1", "code": "x", "user": {"username": "u"}, "caption": {"text": ""}}
    payload = {"require": [["RelayPrefetchedStreamCache", "next", [], ["key", {"__bbox": {"result": {"data": {
        "searchResults": {"edges": [{"node": {"thread": {"thread_items": [{"post": post}, {"post": reply}]}}}]}}}}}]]]}
    html = ('<html><script type="application/json" data-content-len="1" data-sjs>' + json.dumps(payload)
            + '</script><script type="application/json" data-sjs>{"other": 1}</script></html>')
    (lead,) = parse_threads_html(html)
    assert lead.source == "threads" and lead.external_id == "4016990784607610790"
    assert lead.url == "https://www.threads.net/@dexoni.brand/post/De_N9Xujeem"
    assert lead.author == "@dexoni.brand" and lead.found_at.year == 2026


def test_parse_kwork_html():
    want = {"id": 3272347, "isWantActive": True, "name": "Написать ТГ бота", "category_id": "41",
            "description": "Привет! Нужен бот &quot;для записи&quot;.\r\nСрок — неделя.", "priceLimit": "3000.00",
            "possiblePriceLimit": 5000, "date_create": "2026-11-09 10:52:56",
            "wantUserGetProfileUrl": "https://kwork.ru/user/renat_expert", "user": {"username": "renat_expert"}}
    archived = {"id": 1, "isWantActive": False, "name": "Старый", "description": "x"}
    state = {"wantsListData": {"pagination": {"current_page": 1, "data": [want, archived]}}}
    html = "<html><script>window.stateData = " + json.dumps(state, ensure_ascii=False) + ";\n</script></html>"
    (lead,) = parse_kwork_html(html)
    assert lead.external_id == "3272347" and lead.url == "https://kwork.ru/projects/3272347"
    assert lead.title == "Написать ТГ бота" and 'Нужен бот "для записи".\nСрок — неделя.' in lead.text
    assert "Бюджет: до 3000 ₽" in lead.text and lead.contact.endswith("renat_expert")
    assert lead.intent_implied and lead.found_at.isoformat().startswith("2026-11-09T10:52:56+03:00")


def test_parse_weblancer_html():
    jobs = [{"id": "1270449", "type": "project", "name": "Интегратор CRM для бюро", "status": "opened",
             "description": "<p>Нужно настроить <b>CRM</b></p>", "budget": "2400", "bidding_time": "1794215251",
             "url": "/freelance/prikladnoe-po-23/integrator-1270449/", "user": {"first_name": "Владислав", "last_name": ""}},
            {"id": "2", "type": "project", "name": "Закрыт", "status": "closed", "description": "x"}]
    payload = '{"props":{"jobs":' + json.dumps(jobs, ensure_ascii=False) + ',"total":2}}'
    # RSC-payload режется на куски и экранируется как JS-строки
    half = len(payload) // 2
    chunks = [json.dumps(payload[:half])[1:-1], json.dumps(payload[half:])[1:-1]]
    html = "".join(f'<script>self.__next_f.push([1,"{c}"])</script>' for c in chunks)
    (lead,) = parse_weblancer_html(html)
    assert lead.external_id == "1270449" and lead.title == "Интегратор CRM для бюро"
    assert lead.text == "Нужно настроить CRM\nБюджет: 2400 $" and lead.author == "Владислав"
    assert lead.url == "https://www.weblancer.net/freelance/prikladnoe-po-23/integrator-1270449/"


FREELANCE_RU = """<article class="task-card"><div class="task-card__main">
<a class="task-card__title-link" href="/task/view/14957" title="t"> Модернизировать сайт на Тильда </a>
<p class="task-card__desc">Цель: сделать сайт <br /> привлекательнее, используя ИИ</p>
<div class="task-card__chips"><span class="task-chip task-chip--cat">Веб-разработка и IT</span></div>
<span class="task-card__foot-item" title="08.11.2026 17:49">21 час назад</span>
<div class="task-card__budget task-card__budget--soft"><span class="bold">30 000 ₽</span></div>
</div></article><article class="task-card"><p>без ссылки</p></article>"""


def test_parse_freelance_ru_html():
    (lead,) = parse_freelance_ru_html(FREELANCE_RU)
    assert lead.external_id == "14957" and lead.url == "https://freelance.ru/task/view/14957"
    assert lead.title == "Модернизировать сайт на Тильда"
    assert "привлекательнее, используя ИИ" in lead.text and "Категория: Веб-разработка и IT" in lead.text
    assert "Гонорар: 30 000 ₽" in lead.text
    assert lead.found_at.isoformat().startswith("2026-11-08T17:49:00+03:00")


def test_parse_yandex_items_and_leads():
    items = [
        {"type": "business", "id": "1", "title": "Мокко", "address": "Тверская, 1", "urls": ["https://mokko.ru/?yclid=1"],
         "phones": [{"number": "+7 (916) 000-00-00"}], "categories": [{"name": "Кофейня"}]},
        {"type": "business", "id": "2", "title": "Вита-Дент", "address": "Чапаева, 42",
         "phones": [{"number": "+7 (4872) 44-03-40"}], "categories": [{"name": "Стоматология"}],
         "socialLinks": [{"href": "https://t.me/vitadent"}], "ratingData": {"ratingValue": 4.7, "reviewCount": 12}},
        {"type": "collection", "id": "3", "title": "Подборка"},
    ]
    html = '<script>{"x":1,"items":' + json.dumps(items, ensure_ascii=False) + ',"y":2}</script>'
    parsed = parse_yandex_items(html)
    assert [x["id"] for x in parsed] == ["1", "2"]
    lead = business_lead(parsed[1], "стоматология", "Тула")
    assert lead.preset_score == 6 and lead.contact == "+7 (4872) 44-03-40, https://t.me/vitadent"
    assert "Рейтинг 4.7" in lead.text and lead.url == "https://yandex.ru/maps/org/2"
    assert clean_site(parsed[0]["urls"][0]) == "https://mokko.ru"
    audit = audit_lead(parsed[0], "https://mokko.ru", "без HTTPS", "Москва")
    assert audit.source == "site_audit" and audit.external_id == "audit:1" and audit.preset_score == 5
    assert "без HTTPS" in audit.text


def test_parse_instagram_tag_info():
    data = {"data": {"recent": {"sections": [{"layout_content": {"medias": [
        {"media": {"pk": "9", "code": "abc", "taken_at": 1793082639, "user": {"username": "anna"},
                   "caption": {"text": "Ищу кто сделает сайт #нуженсайт"}}},
        {"media": {"pk": "10", "code": "def", "caption": None}},
    ]}}]}}}
    (lead,) = parse_tag_info(data, "нуженсайт")
    assert lead.url == "https://www.instagram.com/p/abc/" and lead.author == "@anna" and lead.title == "#нуженсайт"
