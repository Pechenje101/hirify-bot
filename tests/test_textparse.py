"""Тесты парсера свободного текста вакансии (app/textparse.py, команда /card).

Проверяем три слоя:
1) parse_salary — деньги во всех встречающихся написаниях, и НЕ цепляем
   числа, которые зарплатой не являются (годы опыта, версии);
2) parse_vacancy_text — заголовок/компания/формат/грейд/локация/скиллы/контакты;
3) сборка карточки — распознанная вакансия обязана отрисовываться во всех
   трёх видах (классика, rich, пузырь) без исключений и переполнений.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bubble import assert_widget_fits, render_bubble_html  # noqa: E402
from app.cards import render_classic_html, render_rich_html  # noqa: E402
from app.textparse import parse_salary, parse_vacancy_text  # noqa: E402

SAMPLE = (
    "Senior Solidity Engineer | ChainX Labs\n"
    "📍 Location: Dubai / Global Remote\n"
    "Полная занятость, полностью удалённо.\n"
    "ЗП: 4500-6500 USD/мес + опционы.\n"
    "Стек: Solidity, Foundry, EVM, DeFi-протоколы.\n"
    "Писать рекрутеру: @chainx_hr\n"
    "Опыт разработки смарт-контрактов от 3 лет."
)


# ---------------------------------------------------------------- зарплата

def test_salary_ranges_and_currencies():
    assert parse_salary("ЗП 3-5k$") == "3 000 – 5 000 $"
    assert parse_salary("$3000–5000") == "3 000 – 5 000 $"
    assert parse_salary("от $3000 до 5000 в месяц") == "3 000 – 5 000 $/мес"
    assert parse_salary("зарплата 2500 USDT") == "2 500 USDT"
    assert parse_salary("150 000-200 000 руб.") == "150 000 – 200 000 ₽"
    assert parse_salary("100 000 ₽/мес") == "100 000 ₽/мес"


def test_salary_prefixes_and_k_suffix():
    assert parse_salary("от 150к") == "от 150 000 ₽"      # кириллическое к → ₽
    assert parse_salary("до 200к руб") == "до 200 000 ₽"
    assert parse_salary("от 3.5k$") == "от 3 500 $"
    assert parse_salary("100k+") == "от 100 000 $"
    assert parse_salary("$25/час") == "25 $/час"
    assert parse_salary("от 5k в неделю") == "от 5 000 $/нед"


def test_salary_ignores_non_money_numbers():
    # «от 3 лет опыта» — число без валюты/k, пропускаем и находим настоящую зарплату
    assert parse_salary("от 3 лет опыта, потом 3-5k$") == "3 000 – 5 000 $"
    # вообще без денег — пустая строка, а не мусор
    assert parse_salary("опыт от 5 лет") == ""
    assert parse_salary("скилл 2026 года, версия 3.2") == ""


# ---------------------------------------------------------------- сборка

def test_parse_full_sample():
    v = parse_vacancy_text(SAMPLE)
    assert v.title == "Senior Solidity Engineer"
    assert v.company == "ChainX Labs"
    assert v.salary == "4 500 – 6 500 USD/мес"
    assert v.work_format == "Global Remote"
    assert v.grade == "Senior"
    assert v.employment_type == "Full-time"
    assert v.location == "Dubai / Global Remote"
    assert "solidity" in [s.lower() for s in v.skills]
    assert v.primary_contact and v.primary_contact.display == "@chainx_hr"
    # заголовок и строка локации ушли из описания, тело — на месте
    assert "Senior Solidity Engineer" not in v.description
    assert "Location:" not in v.description
    assert "DeFi-протоколы" in v.description


def test_parse_labels_and_inline_company():
    v = parse_vacancy_text(
        "Должность: Middle React Developer\n"
        "Компания: Yandex\n"
        "Удалённо, part-time.\n"
        "React, TypeScript. Контакты: https://t.me/hr_yndx"
    )
    assert v.title == "Middle React Developer"
    assert v.company == "Yandex"
    assert v.grade == "Middle"
    assert v.employment_type == "Part-time"
    assert v.primary_contact and v.primary_contact.value == "hr_yndx"
    v2 = parse_vacancy_text("Ищем разработчика в компанию Vega Protocol, remote, $2000")
    assert v2.company == "Vega Protocol"


def test_parse_mentions_and_user_ids():
    v = parse_vacancy_text(
        "QA Engineer, remote\nПишите @qa_hr_bot или tg://user?id=123456789"
    )
    kinds = {(c.kind, c.value) for c in v.contacts}
    assert ("username", "qa_hr_bot") in kinds
    assert ("user_id", "123456789") in kinds


def test_parse_grade_cyrillic_and_employment():
    v = parse_vacancy_text("Джун Python разработчик, стажировка, офис в Москве")
    assert v.grade == "Junior"
    assert v.employment_type == "Internship"
    assert v.work_format == "Onsite"


def test_parse_fallbacks_on_garbage():
    v = parse_vacancy_text("   \n\nпросто какой-то текст без вакансии вообще   ")
    assert v.title  # что-то не пустое всегда
    assert v.description
    v2 = parse_vacancy_text("")
    assert v2.title == "Вакансия"
    assert v2.description == ""


def test_job_id_stable_and_far_from_hirify():
    v1 = parse_vacancy_text(SAMPLE)
    v2 = parse_vacancy_text(SAMPLE)
    assert v1.job_id == v2.job_id          # детерминированно
    assert v1.job_id >= 700_000_000        # не пересекается с id фида (~1,4 млн)


def test_underscores_in_usernames_survive():
    v = parse_vacancy_text(
        "DevOps Engineer | Acme\nremote, $3k\nПисать: @acme_hr_bot (t.me/acme_hr_bot)"
    )
    assert v.primary_contact and v.primary_contact.value == "acme_hr_bot"


def test_markdown_bold_stripped():
    v = parse_vacancy_text("**Senior Rust Developer** в команду, remote, от 4k$")
    assert v.title == "Senior Rust Developer"


# ---------------------------------------------------------------- рендеры

def test_parsed_vacancy_renders_all_modes():
    v = parse_vacancy_text(SAMPLE)

    html = render_classic_html(v)
    assert "Senior Solidity Engineer" in html
    assert "4 500 – 6 500 USD/мес" in html
    assert "chainx_hr" in html

    rich = render_rich_html(v)
    assert "Senior Solidity Engineer" in rich
    assert "USD/мес" in rich

    bubble = render_bubble_html(v)
    assert "Senior Solidity Engineer" in bubble
    assert_widget_fits(bubble)  # ≤32768 байт / ≤4096 после zlib-9


def test_parsed_vacancy_without_salary_and_contacts_renders():
    v = parse_vacancy_text("Помощник ассистента, гибрид, обязанности разные")
    assert v.salary == ""
    assert not v.contacts
    html = render_classic_html(v)
    assert "Помощник ассистента" in html
    rich = render_rich_html(v)
    assert "Помощник" in rich


def test_card_texts_format_clean():
    # все плейсхолдеры сводки заполняются, фигурных скобок не остаётся
    from app import texts
    s = texts.CARD_SUMMARY.format(title="T", company="C", salary="S",
                                  contact="K", display="D")
    assert "{" not in s and "}" not in s
    assert "<code>" in texts.CARD_HINT
