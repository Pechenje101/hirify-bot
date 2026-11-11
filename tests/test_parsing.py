"""Тесты парсера на синтетических страницах, повторяющих структуру hirify.me."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.parsing import (  # noqa: E402
    detect_work_format,
    extract_job_links,
    parse_job_page,
)

FEED_HTML = """
<html><body>
<div class="feed">
  <a href="/jobs/1346103-veb-razrabotcik-avtomatizaciia-i-parsing">Веб-разработчик</a>
  <a href="/jobs/1261754-kotlin-parser-developer">Kotlin parser</a>
  <a href="https://hirify.me/jobs/471829-api-integration-engineer">API engineer</a>
  <a href="/jobs/1346103-veb-razrabotcik-avtomatizaciia-i-parsing">дубль ссылки 1</a>
  <a href="/jobs/9999999-">битая ссылка</a>
  <a href="/companies/foo">не вакансия</a>
</div>
</body></html>
"""

JOB_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<title>Разработчик парсеров на Kotlin — CampusApp — Hirify</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Разработчик парсеров на Kotlin",
 "description":"<p>Что делать: разрабатывать парсеры.</p><p>Пишите <b>@campusapp_hr</b> в Telegram или на почту hr@campusapp.io</p>",
 "datePosted":"2026-11-08","employmentType":"FULL_TIME",
 "hiringOrganization":{"@type":"Organization","name":"CampusApp"},
 "baseSalary":{"@type":"MonetaryAmount","currency":"RUB",
   "value":{"@type":"QuantitativeValue","minValue":120000,"maxValue":160000,"unitText":"MONTH"}},
 "jobLocationType":"TELECOMMUTE",
 "applicantLocationRequirements":{"@type":"Country","name":"Russia"},
 "skills":"kotlin, java, api"}
</script>
</head>
<body>
<header><a href="/">Hirify</a><a href="https://t.me/hirify_jobs">наш канал</a></header>
<main>
  <h1>Разработчик парсеров на Kotlin</h1>
  <div class="job-contacts-block">
    <span class="contact-label">Контакты:</span>
    <a href="https://t.me/campusapp_hr">@campusapp_hr</a>
    <a href="tg://resolve?domain=campusapp_hr">tg</a>
    <a href="https://t.me/campus_jobs/777">оригинал поста</a>
  </div>
  <p>Source - Telegram channel</p>
  <footer>© hirify.me</footer>
</main>
</body></html>"""

JOB_HIDDEN_HTML = """<!DOCTYPE html>
<html><head><title>Веб-разработчик — Hirify</title></head>
<body>
<h1>Веб-разработчик (автоматизация и парсинг)</h1>
<p>Source - Telegram channel. Name available after authorization</p>
<button>Show contacts</button>
<style>@media (max-width: 600px) { .x { color: red } }</style>
</body></html>"""

JOB_USERID_HTML = """<!DOCTYPE html>
<html><head><script type="application/ld+json">
{"@type":"JobPosting","title":"Backend dev","description":"Пишите в личку",
 "hiringOrganization":{"name":"ACME"},"datePosted":"2026-11-10"}
</script></head><body>
<div class="contacts"><a href="tg://user?id=123456789">Написать HR</a></div>
</body></html>"""


# ------------------------------------------------------------------ фид

def test_extract_job_links():
    links = extract_job_links(FEED_HTML)
    ids = [jid for jid, _, _ in links]
    assert ids == [1346103, 1261754, 471829], ids
    # url собирается правильно
    _, slug, url = links[0]
    assert slug == "veb-razrabotcik-avtomatizaciia-i-parsing"
    assert url == "https://hirify.me/jobs/1346103-veb-razrabotcik-avtomatizaciia-i-parsing"


# ------------------------------------------------------------------ вакансия

def test_parse_job_page_full():
    url = "https://hirify.me/jobs/1261754-kotlin-parser-developer"
    v = parse_job_page(JOB_HTML, url)
    assert v.job_id == 1261754
    assert v.title == "Разработчик парсеров на Kotlin"
    assert v.company == "CampusApp"
    assert "120 000" in v.salary and "160 000" in v.salary
    assert v.work_format.startswith("Remote")
    assert v.date_posted == "2026-11-08"
    assert v.employment_type == "FULL_TIME"
    assert v.source_type == "telegram"
    assert v.has_direct_contact
    # JSON-LD говорит TELECOMMUTE — это удалёнка, не офис
    assert not v.is_non_remote

    kinds = {(c.kind, c.value.lower()) for c in v.contacts}
    # личный контакт найден и по ссылке, и по tg://resolve — это один и тот же
    assert ("username", "campusapp_hr") in kinds
    # пост в канале — не личный контакт
    assert any(c.kind == "channel" for c in v.contacts)
    # промо-канал hirify отфильтрован
    assert all(c.value.lower() != "hirify_jobs" for c in v.contacts)
    # primary — личный username из блока контактов
    p = v.primary_contact
    assert p and p.kind == "username" and p.value == "campusapp_hr"
    assert p.url == "https://t.me/campusapp_hr"
    assert p.source == "contacts"


def test_parse_hidden_contacts():
    v = parse_job_page(JOB_HIDDEN_HTML, "https://hirify.me/jobs/1346103-veb-razrabotcik")
    assert v.contacts_hidden, "должен распознать скрытые контакты"
    assert not v.has_direct_contact
    assert v.title  # заголовок из h1
    # @media из <style> не должен попасть в контакты
    assert all(c.value.lower() != "media" for c in v.contacts)


def test_parse_userid_contact():
    v = parse_job_page(JOB_USERID_HTML, "https://hirify.me/jobs/555-backend-dev")
    p = v.primary_contact
    assert p and p.kind == "user_id" and p.value == "123456789"
    assert p.url == "tg://user?id=123456789"
    assert v.has_direct_contact


def test_mentions_with_blocklist():
    html = JOB_HTML.replace("@campusapp_hr", "@campusapp_hr и @media и @param")
    v = parse_job_page(html, "https://hirify.me/jobs/1261754-kotlin-parser-developer")
    vals = [c.value.lower() for c in v.contacts if c.kind == "username"]
    assert "campusapp_hr" in vals
    assert "media" not in vals and "param" not in vals


# --------------------------------------------------------------- фильтры разделов

def test_detect_work_format():
    assert detect_work_format("Blockchain Dev · Global Remote · $5k") == "Global Remote"
    assert detect_work_format("Полностью удалённая работа, РФ") == "Remote"
    assert detect_work_format("Some vacancy page without chips") == ""
    assert detect_work_format("Frontend dev · Hybrid · Berlin") == "Hybrid"
    assert detect_work_format("Dev в офисе, Москва") == "Onsite"
    assert detect_work_format("Менеджер по продукту (Onsite, Кипр)") == "Onsite"
    # если в шапке есть и удалёнка, и офис — трактуем как удалёнку (Remote раньше в порядке)
    assert detect_work_format("Можно удалённо или в офисе") == "Remote"


def test_non_remote_evidence():
    from app.parsing import Vacancy

    v = Vacancy(job_id=1, slug="a", url="u", work_format="Hybrid")
    assert v.is_non_remote
    v2 = Vacancy(job_id=2, slug="b", url="u", work_format="Remote (Global)")
    assert not v2.is_non_remote
    v3 = Vacancy(job_id=3, slug="c", url="u", work_format="")
    assert not v3.is_non_remote  # нет признаков — не выкидываем
    # «офисе»/«в офисе» ловится, «официальный» — нет
    v4 = Vacancy(job_id=4, slug="d", url="u", work_format="Работа в офисе")
    assert v4.is_non_remote
    v5 = Vacancy(job_id=5, slug="e", url="u", work_format="Официальное оформление")
    assert not v5.is_non_remote


def test_crypto_detection():
    from app.parsing import Vacancy

    def vac(title="", skills=None, description="", company=""):
        return Vacancy(
            job_id=1, slug="s", url="u", title=title,
            skills=skills or [], description=description, company=company,
        )

    # позитивные
    assert vac(title="Senior Solidity Engineer").is_crypto
    assert vac(title="Backend Developer (Web3)").is_crypto
    assert vac(title="Blockchain Dev").is_crypto
    assert vac(skills=["python", "blockchain"]).is_crypto
    assert vac(title="Трейдер-аналитик", description="Алгоритмический трейдинг на криптовалютном рынке").is_crypto
    assert vac(company="Binance").is_crypto
    assert vac(title="Community Manager", company="OKX").is_crypto
    assert vac(title="NFT Product Manager").is_crypto
    assert vac(title="Разработчик смарт-контрактов").is_crypto
    assert vac(title="Finance Lead", description="Зарплата выплачивается в USDT").is_crypto
    # негативные
    assert not vac(title="Python Backend Developer", description="CRUD API, постгрес, docker").is_crypto
    assert not vac(title="Менеджер по продажам", description="B2B-сегмент, телефония").is_crypto
    assert not vac(title="Security Engineer", description="Криптография и защита данных").is_crypto
    assert not vac(title="Data Engineer", description="define metrics and dashboards").is_crypto
    assert not vac(title="Бухгалтер", description="Дефицит кадров в отделе").is_crypto
    assert not vac(title="Product Manager", description="Fintech-проект, платежи, эквайринг").is_crypto


def test_target_industry_detection():
    from app.parsing import Vacancy

    def vac(title="", skills=None, description="", company=""):
        return Vacancy(
            job_id=1, slug="s", url="u", title=title,
            skills=skills or [], description=description, company=company,
        )

    # позитивные: iGaming / gambling / betting / casino
    assert vac(title="Backend Developer (iGaming)").is_target_industry
    assert vac(title="Sportsbook Data Engineer").is_target_industry
    assert vac(company="Betting Group Ltd").is_target_industry
    assert vac(title="Frontend Dev", company="Rocket Casino").is_target_industry
    assert vac(title="Copywriter", description="казино, покер, рулетка").is_target_industry
    assert vac(title="Аналитик", description="букмекерская контора, ставки на спорт").is_target_industry
    # NDA-проекты / high-risk
    assert vac(title="Java Developer", description="проект под NDA, Кипр").is_target_industry
    assert vac(title="Risk Analyst", description="high-risk payment processing").is_target_industry
    assert vac(title="Инженер", description="высокие риски, онлайн-казино").is_target_industry
    # дейтинг
    assert vac(title="Backend Developer, Dating App").is_target_industry
    assert vac(title="Мобильный разработчик", description="сервис знакомств").is_target_industry
    # крипта (смежная) — через CRYPTO_RE
    assert vac(title="Solidity Engineer").is_target_industry
    # негативные: «обычные» компании и ложные друзья
    assert not vac(title="Python Backend Developer", description="CRUD API, постгрес, docker").is_target_industry
    assert not vac(title="Unity Developer", description="mobile gaming studio").is_target_industry  # геймдев ≠ iGaming
    assert not vac(title="Финансовый аналитик", description="процентные ставки").is_target_industry
    assert not vac(title="HR-менеджер", description="выберите удобный слот для созвона").is_target_industry
    assert not vac(title="Data Engineer", description="define metrics and dashboards").is_target_industry


def test_feed_fields_on_vacancy():
    from app.parsing import Vacancy

    v = Vacancy(job_id=9, slug="s", url="u", feed_key="global", feed_label="🌍 Глобальная удалёнка")
    assert v.feed_key == "global"
    assert "Глобальная" in v.feed_label
