import pytest

from leadhunter.dedup import NearDuplicateIndex
from leadhunter.discovery import score_messages
from leadhunter.filters import evaluate, parse_rule
from leadhunter.geo import geo_allows, mentioned_cities


def test_rule_required_group_and_forbidden_with_wordforms():
    r = parse_rule("+сайт +(нужен|ищу|заказать) -предлагаю")
    assert r.matches("Ищем кто сделает сайты для сети кофеен")
    assert r.matches("Хочу заказать сайт-визитку")
    assert not r.matches("Предлагаю сделать сайт недорого, нужен заказчик")
    assert not r.matches("Нужен логотип")


def test_rule_exact_and_phrase():
    r = parse_rule('"под ключ" +сайт')
    assert r.matches("Сайт под ключ нужен срочно")
    assert not r.matches("Сайт нужен, ключи потеряли")
    r2 = parse_rule("+(телеграм бот|чат-бот)")
    assert r2.matches("Нужен телеграм бот для записи")
    assert r2.matches("ищу чат-бота для сайта")


def test_block_rule_and_evaluate():
    exprs = ["-курс -вебинар", "+сайт +нужен"]
    assert evaluate("Нужен сайт для кофейни", exprs) == ("+сайт +нужен", False)
    assert evaluate("Нужен сайт — приходите на курс", exprs) == (None, True)
    assert evaluate("Продам гараж", exprs) == (None, False)


def test_parse_rule_errors():
    with pytest.raises(ValueError):
        parse_rule("   ")


def test_geo():
    assert mentioned_cities("Ищу разработчика в Туле, можно удалённо") == {"тула"}
    assert geo_allows("Нужен сайт в Туле", ["Тула", "Москва"])
    assert not geo_allows("Нужен сайт в Казани", ["Тула"])
    assert geo_allows("Нужен сайт, город не важен", ["Тула"])
    assert geo_allows("Нужен сайт в Казани", [])


def test_near_duplicates():
    idx = NearDuplicateIndex()
    assert not idx.is_duplicate("Нужен сайт для салона красоты, бюджет 30 тысяч, срок две недели")
    assert idx.is_duplicate("Салону красоты нужен сайт, бюджет 30 тысяч, срок — две недели!")
    assert not idx.is_duplicate("Ищу аниматора для детского мультика про динозавров")


def test_chat_scoring():
    demand = ["Нужен сайт для магазина, кто сделает?", "Посоветуйте разработчика бота", "Сколько стоит лендинг?"]
    supply = ["Сделаю сайт недорого, портфолио в лс", "Предлагаю услуги таргетолога", "Скидка на разработку"]
    assert score_messages(demand) > 60
    assert score_messages(supply) < 30
    assert score_messages([]) == 0
