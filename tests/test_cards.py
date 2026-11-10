"""Тесты рендера карточек в стилистике Apple (Rich HTML + классика + клавиатура)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cards import (  # noqa: E402
    _clean_label,
    _fmt_freshness,
    _format_badge,
    _fmt_date,
    _teaser,
    demo_vacancy,
    inline_keyboard,
    render_classic_html,
    render_rich_html,
)

DEMO = demo_vacancy()

# Кнопок, которые пользователь попросил убрать, быть не должно нигде
REMOVED_BUTTONS = ("Пост в канале", "Вакансия на Hirify", "Вакансия на hirify")


def test_rich_html_structure():
    html = render_rich_html(DEMO)
    # «яблочная» чистая иерархия: заголовок без эмодзи, шапка, жирная зарплата
    assert html.startswith("<h3>")
    assert "<h3>Senior Solidity Engineer (Demo)</h3>" in html
    assert "<b>ChainDemo Labs</b>" in html
    assert "Глобальная удалёнка" in html          # раздел — текстом, без эмодзи
    assert "🌍" not in html and "💼" not in html   # декоративных эмодзи нет (👤 — функциональный)
    assert "<p><b>4 500 – 6 500 USD/мес</b></p>" in html
    # таблица фактов с текстовыми подписями
    assert "<table compact>" in html
    assert "Формат" in html and "Global Remote" in html
    assert "Локация" in html and "Стек" in html
    # дата публикации — не в таблице, а в футере со свежестью
    assert "Опубликовано" not in html
    assert "<hr/>" in html  # разделитель фактов и описания
    assert "<footer>hirify.me · " in html and html.rstrip().endswith("</footer>")
    # описание сворачивается
    assert "<details><summary>" in html
    # контакт-панель (blockquote = подложка с акцентом в клиентах) и кнопки-действия
    assert "<blockquote>👤 Рекрутер: <b>@demo_recruiter</b><br/>" in html
    assert '<tg-button type="url" style="success" url="https://t.me/demo_recruiter">Написать рекрутеру</tg-button>' in html
    assert '<tg-button type="copy_text" text="@demo_recruiter">Копировать контакт</tg-button>' in html
    # удалённые кнопки действительно удалены
    for btn in REMOVED_BUTTONS:
        assert btn not in html, f"кнопка «{btn}» должна быть убрана"
    assert "tg-button" in html  # но сами кнопки-действия есть


def test_rich_extra_contacts_collapsible():
    """Если у вакансии несколько TG-контактов — лишние уходят в сворачиваемый блок."""
    html = render_rich_html(DEMO)  # в демо два контакта: @demo_recruiter + @chaindemo_hr
    assert "Ещё Telegram-контакты: 1" in html
    assert 'href="https://t.me/chaindemo_hr"' in html
    assert "@chaindemo_hr" in html
    # основной контакт — НЕ в сворачиваемом блоке, а в панели рекрутера
    assert "@demo_recruiter</b>" in html


def test_rich_single_contact_has_no_extra_block():
    from app.parsing import TGContact, Vacancy

    v = Vacancy(
        job_id=8, slug="one", url="https://hirify.me/jobs/8-one", title="One contact",
        contacts=[TGContact("username", "only_hr", "@only_hr", "https://t.me/only_hr", "contacts")],
    )
    html = render_rich_html(v)
    assert "Ещё Telegram-контакты" not in html


def test_rich_html_badges_and_dates():
    assert _format_badge(DEMO) == "Global Remote"  # без эмодзи — чистый текст
    assert _fmt_date("2026-11-11T10:00:00+00:00") == "11.11.2026"
    assert _fmt_date("") == ""


def test_freshness_relative_dates():
    """Свежесть в футере: сегодня/вчера/N дня(ей) назад, старше недели — дата."""
    today = date(2026, 11, 11)
    assert _fmt_freshness("2026-11-11", today) == "опубликовано сегодня"
    assert _fmt_freshness("2026-11-11T23:59:00+00:00", today) == "опубликовано сегодня"
    assert _fmt_freshness("2026-11-10", today) == "опубликовано вчера"
    assert _fmt_freshness("2026-11-08", today) == "опубликовано 3 дня назад"
    assert _fmt_freshness("2026-11-07", today) == "опубликовано 4 дня назад"
    assert _fmt_freshness("2026-11-05", today) == "опубликовано 6 дней назад"
    assert _fmt_freshness("2026-11-01", today) == "опубликовано 01.11.2026"
    assert _fmt_freshness("2026-10-01", today) == "опубликовано 01.10.2026"
    assert _fmt_freshness("", today) == ""
    assert _fmt_freshness("мусор", today) == ""
    # будущее/битое — не падаем, считаем «сегодня»
    assert _fmt_freshness("2026-11-12", today) == "опубликовано сегодня"
    assert _fmt_freshness("2026-13-99", today) == ""


def test_footer_line_without_date():
    from app.cards import _footer_line
    from app.parsing import Vacancy

    v = Vacancy(job_id=1, slug="s", url="u", title="x")
    assert _footer_line(v) == "hirify.me"
    v2 = demo_vacancy()
    assert _footer_line(v2).startswith("hirify.me · опубликовано ")


def test_rich_footer_and_hr_in_full_card():
    html = render_rich_html(DEMO)
    # ритм: таблица → hr → описание; кнопки → футер
    assert "</table>\n<hr/>" in html
    footer = html.rstrip().rsplit("\n", 1)[-1]
    assert footer.startswith("<footer>hirify.me · опубликовано ") and footer.endswith("</footer>")


def test_minimal_card_has_footer_but_no_hr():
    """Без фактов hr не нужен, но футер со свежестью появляется всегда."""
    from app.parsing import TGContact, Vacancy

    v = Vacancy(
        job_id=5, slug="m", url="https://hirify.me/jobs/5-m", title="Minimal",
        contacts=[TGContact("username", "m_hr", "@m_hr", "https://t.me/m_hr", "contacts")],
    )
    html = render_rich_html(v)
    assert "<hr/>" not in html
    assert "<footer>hirify.me</footer>" in html


def test_clean_label_strips_leading_emoji():
    assert _clean_label("🏠 Удалёнка") == "Удалёнка"
    assert _clean_label("🌍 Глобальная удалёнка") == "Глобальная удалёнка"
    assert _clean_label("🪙🌍 Crypto Remote") == "Crypto Remote"
    assert _clean_label("Все вакансии") == "Все вакансии"
    assert _clean_label("") == ""


def test_teaser():
    long = "Первое предложение. Второе предложение! " * 20
    t = _teaser(long, maxlen=120)
    assert len(t) <= 125 and t.endswith("…")
    short = "Короткий текст."
    assert _teaser(short) == short
    assert _teaser("") == ""


def test_rich_html_escapes_dangerous_text():
    from app.parsing import TGContact, Vacancy

    v = Vacancy(
        job_id=1, slug="x", url="https://hirify.me/jobs/1-x",
        title='Senior <script>alert(1)</script> Dev',
        description="Опыт с <b>HTML</b> и &符号 обязателен",
        contacts=[TGContact("username", "hr_x", "@hr_x", "https://t.me/hr_x", "contacts")],
    )
    html = render_rich_html(v)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;符号" in html or "&#x" in html  # амперсанд экранирован


def test_rich_html_truncates_description():
    from app.parsing import TGContact, Vacancy

    v = Vacancy(
        job_id=2, slug="y", url="https://hirify.me/jobs/2-y",
        description=("очень длинный абзац\n\n" * 500),
        contacts=[TGContact("username", "hr_y", "@hr_y", "https://t.me/hr_y", "contacts")],
    )
    html = render_rich_html(v, limit=800)
    assert len(html) < 1800
    assert "полный текст — на hirify.me" in html


def test_rich_and_classic_fit_telegram_limit():
    """Весь rich-блок с дефолтным лимитом обязан влезать в 4096 символов Telegram."""
    html = render_rich_html(DEMO, limit=1800)
    assert len(html) <= 4096, f"rich-карточка слишком длинная: {len(html)}"
    classic = render_classic_html(DEMO, limit=1800)
    assert len(classic) <= 4096, f"классическая карточка слишком длинная: {len(classic)}"


def test_classic_html():
    html = render_classic_html(DEMO)
    assert html.startswith("<b>")
    assert "<b>Senior Solidity Engineer (Demo)</b>" in html
    assert 'href="https://t.me/demo_recruiter"' in html
    # шапка и зарплата
    assert "<b>4 500 – 6 500 USD/мес</b>" in html
    assert "Глобальная удалёнка" in html
    # факты — панелью-цитатой (blockquote с подложкой в современных клиентах),
    # дата публикации — не в фактах, а в футере
    assert "<blockquote>Формат — Global Remote\nЛокация — из любой точки мира" in html
    assert "Стек — solidity" in html
    assert "Опубликовано" not in html
    # контакт — тоже панелью, с доп. контактами внутри
    assert "<blockquote>👤 Рекрутер — <b>@demo_recruiter</b>" in html
    assert "Ещё: <a href=\"https://t.me/chaindemo_hr\">@chaindemo_hr</a>" in html
    # дата в футере со свежестью
    assert "11.11.2026" in html or "опубликовано" in html
    # сдержанный футер и никаких rich-тегов
    assert html.endswith("</i>") and "<i>hirify.me" in html
    for tag in ("<tg-button", "<details", "<table", "<h3", "<tg-button-row"):
        assert tag not in html, f"rich-тег {tag} попал в классическую карточку"
    assert "<blockquote expandable>" in html  # длинное описание сворачивается
    # удалённых кнопок/ссылок нет
    for btn in REMOVED_BUTTONS:
        assert btn not in html
    assert 'href="https://hirify.me/jobs' not in html  # ссылка на вакансию убрана


def test_classic_long_description_uses_expandable_quote():
    from app.parsing import TGContact, Vacancy

    v = Vacancy(
        job_id=3, slug="z", url="https://hirify.me/jobs/3-z",
        description="строка описания\n\n" * 120,
        contacts=[TGContact("username", "hr_z", "@hr_z", "https://t.me/hr_z", "contacts")],
    )
    html = render_classic_html(v, limit=2400)
    assert "<blockquote expandable>" in html


def test_inline_keyboard():
    kb = inline_keyboard(DEMO)
    rows = kb.inline_keyboard
    assert rows, "клавиатура пуста"
    flat = [b for row in rows for b in row]
    texts = [b.text for b in flat]
    # только два действия: написать и скопировать
    assert any("Написать рекрутеру" in t for t in texts)
    assert any("Копировать контакт" in t for t in texts)
    for btn in REMOVED_BUTTONS:
        assert all(btn not in t for t in texts), f"кнопка «{btn}» должна быть убрана"
    assert len(flat) == 2
    write_btn = next(b for b in flat if "Написать" in (b.text or ""))
    assert write_btn.url == "https://t.me/demo_recruiter"
    copy_btn = next(b for b in flat if "Копировать" in (b.text or ""))
    assert copy_btn.copy_text and copy_btn.copy_text.text == "@demo_recruiter"


def test_keyboard_for_userid_contact_has_no_copy():
    """tg://user?id=... нечего копировать как @username — кнопка копирования не нужна."""
    from app.parsing import TGContact

    v = demo_vacancy()
    v.contacts = [TGContact("user_id", "123456789", "tg://user?id=123456789",
                            "tg://user?id=123456789", "contacts")]
    kb = inline_keyboard(v)
    flat = [b for row in kb.inline_keyboard for b in row]
    assert [b.text for b in flat] == ["✉️ Написать рекрутеру"]
    # панель контакта для user_id — без @username, но с пометкой о прямом контакте
    html = render_rich_html(v)
    assert "👤 <b>Прямой контакт рекрутера</b>" in html
    classic = render_classic_html(v)
    assert "👤 <b>Прямой контакт рекрутера</b>" in classic


def test_no_contact_card_still_renders():
    from app.parsing import Vacancy

    v = Vacancy(job_id=4, slug="n", url="https://hirify.me/jobs/4-n", title="No contacts")
    html = render_rich_html(v)
    assert "Написать рекрутеру" not in html
    kb = inline_keyboard(v)
    assert kb.inline_keyboard == []  # без контактов кнопок нет — и это не падает


def test_format_badge_mapping():
    from app.parsing import Vacancy

    def vfmt(wf: str) -> str:
        return _format_badge(Vacancy(job_id=1, slug="s", url="u", work_format=wf))

    assert vfmt("Remote (Russia)") == "Remote"
    assert vfmt("Global Remote") == "Global Remote"
    assert vfmt("Hybrid") == "Hybrid"
    assert vfmt("Onsite") == "Onsite"
    assert vfmt("Удалённо") == "Remote"
    assert vfmt("") == ""


def test_rich_html_minimal_card_no_optional_fields():
    """Карточка-минимум (только заголовок и контакт) рендерится без пустых блоков."""
    from app.parsing import TGContact, Vacancy

    v = Vacancy(
        job_id=7, slug="m", url="https://hirify.me/jobs/7-m",
        title="Minimal vacancy",
        contacts=[TGContact("username", "min_hr", "@min_hr", "https://t.me/min_hr", "contacts")],
    )
    html = render_rich_html(v)
    assert "<table compact>" not in html          # фактов нет — таблицы нет
    assert "<p><b>" not in html or "Занятость" not in html
    assert "Формат" not in html
    assert "<h3>Minimal vacancy</h3>" in html
    assert "Рекрутер: <b>@min_hr</b>" in html


def test_facts_rows_shared_between_renderers():
    """Rich-таблица и классика строят факты из одного хелпера."""
    from app.cards import _facts_rows

    rows = _facts_rows(DEMO)
    labels = [lbl for lbl, _ in rows]
    assert labels[0] == "Формат"
    assert "Опубликовано" not in labels  # дата — в футере, не в фактах
    assert all(val for _, val in rows), "пустых строк фактов быть не должно"
    rich = render_rich_html(DEMO)
    classic = render_classic_html(DEMO)
    for lbl, val in rows:
        assert lbl in rich and lbl in classic


def test_keyboard_main_button_is_primary_style():
    """Bot API 9.4: главная кнопка — акцентная (primary), копирование — нейтральная."""
    kb = inline_keyboard(DEMO)
    flat = [b for row in kb.inline_keyboard for b in row]
    write_btn = next(b for b in flat if "Написать" in (b.text or ""))
    copy_btn = next(b for b in flat if "Копировать" in (b.text or ""))
    assert write_btn.style == "primary"
    assert not copy_btn.style  # дефолтный стиль приложения
