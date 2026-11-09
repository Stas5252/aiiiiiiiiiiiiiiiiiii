from leadhunter.extract import extract_budget, extract_contacts
from leadhunter.llm import build_examples


def test_extract_contacts():
    text = "Пишите +7 (999) 123-45-67 или на mail@firm.ru, тг @ivan_dev, ссылка https://t.me/other"
    assert extract_contacts(text) == "+7 (999) 123-45-67, mail@firm.ru, @ivan_dev"


def test_extract_contacts_empty():
    assert extract_contacts("просто текст без контактов") == ""


def test_extract_budget():
    assert extract_budget("Бюджет: 50 000 руб, срок неделя") == "50 000 руб"
    assert extract_budget("готов платить 30к за бота") == "30 к"
    assert extract_budget("нужен сайт") == ""


def test_build_examples():
    assert build_examples([], []) == ""
    s = build_examples(["хороший"], ["плохой"])
    assert "ХОРОШИЕ" in s and "- хороший" in s and "НЕ ЛИД" in s and "- плохой" in s
