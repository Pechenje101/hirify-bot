"""Тесты сборки Vacancy из JSON-API hirify.me (фикстуры повторяют реальные ответы)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.parsing import (  # noqa: E402
    TGContact, _api_contacts, _salary_from_api, vacancy_from_api,
)

# Реальный ответ GET /api/vacancies/1342174-telegram-outreach-manager-igaming
# (структура сохранена, значения сокращены)
VACANCY_JSON = {
    "id": "1342174",
    "slug": "1342174-telegram-outreach-manager-igaming",
    "title": "Telegram Outreach Manager (iGaming)",
    "original_title": "TELEGRAM OUTREACH MANAGER",
    "company_title": None,
    "work_format": ["remote"],
    "remote_type": "global",
    "work_type": "fulltime",
    "employee_type": ["employment"],
    "vacancy_language": "ru",
    "text": "<strong>TELEGRAM OUTREACH MANAGER | ID: 2476</strong><br />\nCompany: NDA<br />\nЗадачи: поиск каналов.",
    "tldr": "Менеджер по Telegram-аутричу (iGaming)",
    "source": "telegram",
    "created_at": "2026-10-30T13:01:11.000000Z",
    "is_scam": False,
    "is_potential_scam": False,
    "tags": [{"id": 13, "name": "igaming"}, {"id": 643, "name": "affiliate marketing"}],
    "nice_to_have_tags": ["готовая база контактов"],
    "salary": None,
    "grades": [],
    "regions": [{"id": 1, "code": "russia", "name": "Россия"}],
}

CONTACTS_JSON = {
    "vacancy_id": "1342174",
    "contacts": [
        {"type": "telegram", "value": "https://t.me/iGaming_work/5195", "short_code": "1eHg0"},
    ],
    "company_title": None,
    "linkedin": None,
    "access_type": "free_vacancy",
    "remaining_attempts": None,
}

CONTACTS_USERNAME = {
    "vacancy_id": "2",
    "contacts": [{"type": "telegram", "value": "https://t.me/acme_hr", "short_code": "x1"}],
}


def test_vacancy_from_api_maps_fields():
    v = vacancy_from_api(VACANCY_JSON, CONTACTS_JSON, "remote", "🏠 Удалёнка")
    assert v.job_id == 1342174
    assert v.slug == "1342174-telegram-outreach-manager-igaming"
    assert v.title == "Telegram Outreach Manager (iGaming)"
    assert v.company == ""            # company_title=None → пусто, не «None»
    assert "remote" in v.work_format and "global" in v.work_format
    assert v.employment_type == "fulltime, employment"
    assert v.location == "Россия"
    assert v.source_type == "telegram"
    assert v.date_posted == "2026-10-30"
    assert "igaming" in v.skills and "готовая база контактов" in v.skills
    assert "Задачи" in v.description and "<strong>" not in v.description
    assert v.feed_key == "remote" and v.feed_label == "🏠 Удалёнка"
    assert v.has_direct_contact           # пост в канале — тоже контакт
    assert v.primary_contact.kind == "channel"
    assert v.primary_contact.url == "https://t.me/iGaming_work/5195"
    assert v.primary_contact.display == "@iGaming_work"


def test_personal_username_contact():
    v = vacancy_from_api(VACANCY_JSON, CONTACTS_USERNAME, "remote", "🏠 Удалёнка")
    c = v.primary_contact
    assert c.kind == "username" and c.value == "acme_hr"
    assert c.url == "https://t.me/acme_hr" and c.display == "@acme_hr"
    assert c.is_personal
    assert c.source == "contacts"


def test_hirify_service_contacts_ignored():
    contacts = {"contacts": [
        {"type": "telegram", "value": "https://t.me/hirify_support_bot"},
        {"type": "telegram", "value": "https://t.me/hirifyme_bot"},
        {"type": "telegram", "value": "https://t.me/real_hr"},
    ]}
    got = _api_contacts(contacts)
    assert [c.value for c in got] == ["real_hr"]


def test_salary_from_api_variants():
    assert _salary_from_api(None) == ""
    assert _salary_from_api({"min": 2000, "max": 3500, "currency": "USD", "period": "month"}) \
        == "2 000 – 3 500 USD/мес"
    assert _salary_from_api({"from": 100, "to": 200, "currency_code": "€", "unit": "hour"}) \
        == "100 – 200 €/час"
    assert _salary_from_api({"min": 5000}) == "от 5 000"
    assert _salary_from_api({"max": 900}) == "до 900"
    assert _salary_from_api({"currency": "USD"}) == ""


def test_non_remote_marked():
    data = dict(VACANCY_JSON)
    data["work_format"] = ["onsite"]
    v = vacancy_from_api(data, CONTACTS_JSON)
    assert v.is_non_remote
    data["work_format"] = ["hybrid"]
    assert vacancy_from_api(data, CONTACTS_JSON).is_non_remote
    data["work_format"] = ["remote"]
    assert not vacancy_from_api(data, CONTACTS_JSON).is_non_remote


def test_crypto_signs_from_tags():
    data = dict(VACANCY_JSON)
    data["title"] = "Smart Contract Auditor"
    data["tags"] = [{"id": 1, "name": "solidity"}, {"id": 2, "name": "defi"}]
    v = vacancy_from_api(data, CONTACTS_JSON, "crypto", "🪙 Crypto")
    assert v.is_crypto
    assert v.feed_key == "crypto"


def test_numeric_and_raw_contact_values():
    got = _api_contacts({"contacts": [
        {"type": "telegram", "value": "123456789"},
        {"type": "telegram", "value": "@plain_handle"},
    ]})
    kinds = sorted(c.kind for c in got)
    assert kinds == ["user_id", "username"]
    assert all(isinstance(c, TGContact) for c in got)
