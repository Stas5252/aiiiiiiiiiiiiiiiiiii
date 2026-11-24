"""Модуль B: словарь, намерение, гео, бюджет и контакты, разбор ответа ИИ (находки 8, 13, 17, 18, 23, 24)."""
import httpx
import pytest

from leadhunter.extract import extract_budget, extract_contacts
from leadhunter.geo import geo_allows, mentioned_cities
from leadhunter.llm import LLMClassifier, parse_bool
from leadhunter.scoring import Keywords, score_text


# --- находка 8: слово по теме — это слово, а не начало любого слова ---

@pytest.mark.parametrize("text", [
    "Нужны ботинки зимние, подскажите где заказать недорого",
    "Ищу косметолога, нужен ботокс, подскажите",
    "Ищу роликовые коньки, нужна помощь с выбором",
    "Нужна печать визиток, срочно",
    "Ищу аиста для фотосессии, подскажите",
])
def test_lookalike_words_are_not_topic(text):
    r = score_text(text, Keywords())
    assert r.topic == [] and r.score == 0


@pytest.mark.parametrize("text, topic", [
    ("Нужны боты для записи клиентов", "бот"),
    ("ищу кто сделает чат-бота в телеграме", "чат-бот"),
    ("нужны ролики для соцсетей", "ролик"),
    ("Нужен сайт на Тильде", "тильд"),
    ("нужна автоматизация заявок", "автоматизац"),
    ("нужен ai ассистент", "ai"),
    ("нужен ии-агент для поддержки", "ии-агент"),
    ("нужна интеграция с amoCRM", "amocrm"),
    ("нужен gpt4 помощник", "gpt"),
])
def test_word_forms_still_match(text, topic):
    assert topic in score_text(text, Keywords()).topic


def test_short_latin_stems_match_whole_words_only():
    assert score_text("Нужен air fryer, подскажите", Keywords()).topic == []
    assert score_text("ищу aisle manager", Keywords()).topic == []


def test_verb_keys_keep_their_form():
    """«сделаю» — предложение исполнителя, «сделает» — запрос клиента: формы глаголов не смешиваются."""
    r = score_text("Ищу, кто сделает сайт", Keywords())
    assert r.negative == [] and "кто сделает" in r.intent
    assert "сделаю" in score_text("Сделаю сайт недорого", Keywords()).negative


# --- находка 24: формы намерения ---

@pytest.mark.parametrize("text, key", [
    ("Нужны сайты для двух филиалов", "нужен"),
    ("Требуются разработчики: сделать лендинг", "требуются"),
    ("Необходим лендинг для клиники", "необходим"),
    ("Необходимо сделать сайт", "необходим"),
])
def test_intent_forms(text, key):
    r = score_text(text, Keywords())
    assert key in r.intent and r.score >= 5


def test_one_word_counts_as_one_intent():
    """«нужен», «нужна», «нужно», «нужны» — одно слово: одно совпадение, а не четыре."""
    assert score_text("Нужны сайты", Keywords()).intent == ["нужен"]


# --- находка 13: гео ---

@pytest.mark.parametrize("text", [
    "Меня зовут Владимир, нужен сайт",
    "Нужен грозный дизайн сайта",
    "Нужен сайт про угольные шахты",
    "Нужен лендинг про тур на Бали",
])
def test_names_and_common_words_are_not_cities(text):
    assert mentioned_cities(text) == set()
    assert geo_allows(text, ["Москва"])


def test_ambiguous_city_counts_with_preposition():
    assert mentioned_cities("Нужен сайт, мы в Грозном") == {"грозный"}
    assert mentioned_cities("Кофейня из Владимира ищет подрядчика") == {"владимир"}
    assert mentioned_cities("г. Шахты, нужен сайт") == {"шахты"}
    assert not geo_allows("Нужен сайт, мы во Владимире", ["Москва"])
    assert not mentioned_cities("работаю в шахте, нужен сайт")


def test_city_synonyms():
    assert geo_allows("нужен сайт, я в Питере", ["Санкт-Петербург"])
    assert geo_allows("нужен сайт, СПб", ["Питер"])
    assert geo_allows("нужен сайт в Москве", ["мск"])
    assert geo_allows("нужен сайт, мск", ["Москва"])
    assert geo_allows("нужен сайт в Санкт-Петербурге", ["спб"])
    assert not geo_allows("нужен сайт в Казани", ["мск", "спб"])
    assert not geo_allows("нужен сайт, екб", ["Москва"])


def test_geo_without_city_or_filter_passes():
    assert geo_allows("нужен сайт", ["Москва"])
    assert geo_allows("нужен сайт в Казани", [])


# --- находка 23: бюджет и контакты ---

@pytest.mark.parametrize("text, budget", [
    ("квартира в 2 комнаты, 3 к. дома", ""),
    ("в 2024 р. мы открылись", ""),
    ("нужен сайт за 5-7 тыс", "5–7 тыс"),
    ("бюджет 30 000 – 50 000 руб", "30 000–50 000 руб"),
    ("от 1.5 млн", "1.5 млн"),
    ("бюджет до 2 млн руб", "2 млн"),
    ("готов платить 30к за бота", "30 к"),
    ("Бюджет: 50 000 руб, срок неделя", "50 000 руб"),
    ("бюджет 50к.", "50 к"),
    ("оплата 2000 р. за логотип", "2000 р."),
    ("нужен сайт", ""),
])
def test_budget(text, budget):
    assert extract_budget(text) == budget


def test_tme_link_is_contact():
    assert extract_contacts("пишите t.me/ivan_dev") == "@ivan_dev"
    assert extract_contacts("https://t.me/ivan_dev или @ivan_dev") == "@ivan_dev"
    # приглашения, посты каналов и служебные адреса — не контакт человека
    assert extract_contacts("чат t.me/+AbCdEf12345 и пост https://t.me/somechannel/123, t.me/joinchat/xxxxxx") == ""


# --- находки 17–18: ответ ИИ ---

@pytest.mark.parametrize("value, expected", [
    (True, True), (False, False), ("true", True), ("false", False), ("False", False), ("да", True),
    ("нет", False), ("Нет.", False), (" yes ", True), ("no", False), (1, True), (0, False), (None, False),
    ("", False), ("не знаю", False),
])
def test_parse_bool(value, expected):
    assert parse_bool(value) is expected


def _llm(handler, **kw) -> LLMClassifier:
    llm = LLMClassifier("k", "http://llm.test/v1", "m", **kw)
    llm.client = httpx.AsyncClient(base_url="http://llm.test/v1", transport=httpx.MockTransport(handler))
    return llm


def _answer(content: str):
    return lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_llm_string_false_is_not_a_lead():
    v = await _llm(_answer('{"is_lead": "false", "score": 9, "summary": "реклама"}')).classify("текст", "студия")
    assert v is not None and v.is_lead is False
    v = await _llm(_answer('{"is_lead": "нет", "score": "9"}')).classify("текст", "студия")
    assert v.is_lead is False
    v = await _llm(_answer('{"is_lead": "да", "score": "8/10"}')).classify("текст", "студия")
    assert v.is_lead is True and v.score == 8


async def test_llm_quota_is_visible():
    llm = _llm(_answer('{"is_lead": true, "score": 7}'), max_per_hour=1)
    assert (await llm.classify("первый", "студия")).is_lead
    assert not llm.quota_exhausted
    assert await llm.classify("второй", "студия") is None
    assert llm.quota_exhausted
