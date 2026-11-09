from leadhunter.scoring import Keywords, score_text


def test_request_with_intent_scores_high():
    r = score_text("Ребята, ищу кто сделает сайт под ключ, бюджет 50к", Keywords())
    assert r.score >= 5
    assert "сайт" in r.topic and "ищу" in r.intent


def test_competitor_offer_is_penalized():
    r = score_text("Сделаю сайт быстро и дёшево, портфолио в профиле, пишите в лс", Keywords())
    assert r.score < 5
    assert "сделаю" in r.negative


def test_offtopic_scores_zero():
    assert score_text("Кто знает хороший ресторан в центре?", Keywords()).score == 0


def test_short_stems_match_whole_words_only():
    assert score_text("нужен ии ассистент", Keywords()).topic == ["ии"]
    assert "ии" not in score_text("нужен иисус", Keywords()).topic


def test_yo_normalization_and_stems():
    r = score_text("Нужён лендинг для кофейни", Keywords())
    assert "лендинг" in r.topic and "нужен" in r.intent


def test_intent_implied_for_boards():
    text = "Разработка сайта-визитки для стоматологии"
    assert score_text(text, Keywords()).score == 2
    assert score_text(text, Keywords(), intent_implied=True).score == 5
