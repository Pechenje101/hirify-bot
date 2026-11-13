"""Тесты рендера карточек в стилистике Apple (Rich HTML + классика + клавиатура)."""
import sys
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
REMOVED_BUTTONS = ("Пост в канале", "Вакансия на Hirify", "Вакансия на hirify", "Копировать контакт")


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
    assert "Локация" in html and "Навыки" in html
    # дата публикации — не в таблице, а в футере
    assert "Опубликовано" not in html
    assert "<hr/>" in html  # разделитель фактов и описания
    assert "<footer>опубликовано 11.11.2026</footer>" in html
    assert html.rstrip().endswith("</footer>")
    # описание сворачивается
    assert "<details><summary>" in html
    # контакт-панель (blockquote = подложка с акцентом в клиентах) и кнопки-действия
    assert "<blockquote>👤 Рекрутер: <b>@demo_recruiter</b><br/>" in html
    assert '<tg-button type="url" style="success" url="https://t.me/demo_recruiter">Написать рекрутеру</tg-button>' in html
    # ровно одна кнопка-действие (решение 13.11: копирование убрано)
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


def test_freshness_is_publication_date():
    """Футер — точная дата публикации (DD.MM.YYYY), а не относительная
    «сегодня/вчера/N дней назад»: фид медленный, относительная формулировка
    ничего не сообщала."""
    assert _fmt_freshness("2026-11-11") == "опубликовано 11.11.2026"
    assert _fmt_freshness("2026-11-11T23:59:00+00:00") == "опубликовано 11.11.2026"
    assert _fmt_freshness("2026-11-10") == "опубликовано 10.11.2026"
    assert _fmt_freshness("2026-11-01") == "опубликовано 01.11.2026"
    assert _fmt_freshness("2026-10-01") == "опубликовано 01.10.2026"
    assert _fmt_freshness("2026-01-02") == "опубликовано 02.01.2026"
    assert _fmt_freshness("") == ""
    assert _fmt_freshness("мусор") == ""
    assert _fmt_freshness("2026-13-99") == ""


def test_footer_line_without_date():
    """Футер — только дата публикации; без даты — пустой, без упоминаний источника."""
    from app.cards import _footer_line
    from app.parsing import Vacancy

    v = Vacancy(job_id=1, slug="s", url="u", title="x")
    assert _footer_line(v) == ""
    v2 = demo_vacancy()
    assert _footer_line(v2).startswith("опубликовано ")


def test_rich_footer_and_hr_in_full_card():
    html = render_rich_html(DEMO)
    # ритм: таблица → hr → описание; кнопки → футер (только свежесть, без источника)
    assert "</table>\n<hr/>" in html
    footer = html.rstrip().rsplit("\n", 1)[-1]
    assert footer.startswith("<footer>опубликовано ") and footer.endswith("</footer>")


def test_minimal_card_without_date_has_no_footer_and_no_hr():
    """Без фактов hr не нужен; без даты публикации — нет и футера (не пустого).
    Упоминаний источника вакансий в карточке не остаётся вовсе."""
    from app.parsing import TGContact, Vacancy

    v = Vacancy(
        job_id=5, slug="m", url="https://hirify.me/jobs/5-m", title="Minimal",
        contacts=[TGContact("username", "m_hr", "@m_hr", "https://t.me/m_hr", "contacts")],
    )
    html = render_rich_html(v)
    assert "<hr/>" not in html
    assert "<footer>" not in html


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
    """Явный limit — жёсткий потолок: обрезка без заглушек, просто многоточие."""
    from app.parsing import TGContact, Vacancy

    v = Vacancy(
        job_id=2, slug="y", url="https://hirify.me/jobs/2-y",
        description=("очень длинный абзац\n\n" * 500),
        contacts=[TGContact("username", "hr_y", "@hr_y", "https://t.me/hr_y", "contacts")],
    )
    html = render_rich_html(v, limit=800)
    assert len(html) < 1800
    assert "…" in html
    assert "hirify" not in html.lower()


def test_rich_and_classic_fit_telegram_limit():
    """Автобюджет: rich — целиком в 32768 символов html, классика — в 4096
    видимых символов (после разбора entities)."""
    from app.cards import _visible_len

    html = render_rich_html(DEMO)
    assert len(html) <= 32768, f"rich-карточка слишком длинная: {len(html)}"
    classic = render_classic_html(DEMO)
    assert _visible_len(classic) <= 4096, f"классическая карточка длиннее лимита: {_visible_len(classic)}"

    # и то же для гипотетически огромного описания — автобюджет ужимает
    from app.parsing import TGContact, Vacancy

    v = Vacancy(
        job_id=12, slug="huge", url="https://hirify.me/jobs/12-huge",
        title="Huge",
        description="абзац описания\n\n" * 4000,  # ~80 000 символов
        contacts=[TGContact("username", "huge_hr", "@huge_hr", "https://t.me/huge_hr", "contacts")],
    )
    assert len(render_rich_html(v)) <= 32768
    assert _visible_len(render_classic_html(v)) <= 4096


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
    assert "Навыки — solidity" in html
    assert "Опубликовано" not in html
    # контакт — тоже панелью, с доп. контактами внутри
    assert "<blockquote>👤 Рекрутер — <b>@demo_recruiter</b>" in html
    assert "Ещё: <a href=\"https://t.me/chaindemo_hr\">@chaindemo_hr</a>" in html
    # дата в футере — точная дата публикации
    assert "11.11.2026" in html and "опубликовано" in html
    # сдержанный футер (только дата публикации) и никаких rich-тегов
    assert html.endswith("</i>") and "<i>опубликовано" in html
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


def test_full_description_lives_inside_cards():
    """Полное описание — прямо в карточке (разворачивается), а не заглушка
    «полный текст — на сайте»: хвост описания присутствует и в rich, и в
    классике; упоминаний источника вакансий нет нигде."""
    from app.parsing import TGContact, Vacancy

    tail = "ФИНАЛЬНЫЙ АБЗАЦ: условия, релокация, бонусы и контакты в конце текста."
    v = Vacancy(
        job_id=13, slug="full", url="https://hirify.me/jobs/13-full",
        title="Full Description Role",
        company="Full Corp",
        description="\n\n".join(f"Раздел {i}. Обязанности, стек и процессы команды." for i in range(40))
                    + "\n\n" + tail,
        contacts=[TGContact("username", "full_hr", "@full_hr", "https://t.me/full_hr", "contacts")],
    )
    rich = render_rich_html(v)
    classic = render_classic_html(v)
    assert tail in rich            # rich вмещает всё целиком
    assert tail in classic         # ~1 800 символов — тоже влезает в 4096
    assert "Показать полное описание" in rich
    assert "<blockquote expandable>" in classic
    assert "hirify" not in rich.lower()
    assert "hirify" not in classic.lower()
    # никаких заглушек про «полное описание на сайте»
    for stub in ("полное описание — на", "полный текст — на", "описание на сайте"):
        assert stub not in rich.lower()
        assert stub not in classic.lower()


def test_inline_keyboard():
    kb = inline_keyboard(DEMO)
    rows = kb.inline_keyboard
    assert rows, "клавиатура пуста"
    flat = [b for row in rows for b in row]
    texts = [b.text for b in flat]
    # ровно ОДНО действие — написать (копирование убрано 13.11)
    assert any("Написать рекрутеру" in t for t in texts)
    for btn in REMOVED_BUTTONS:
        assert all(btn not in t for t in texts), f"кнопка «{btn}» должна быть убрана"
    assert len(flat) == 1
    write_btn = flat[0]
    assert write_btn.url == "https://t.me/demo_recruiter"


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
    """Bot API 9.4: единственная кнопка «Написать» — акцентная (primary)."""
    kb = inline_keyboard(DEMO)
    flat = [b for row in kb.inline_keyboard for b in row]
    assert len(flat) == 1
    assert "Написать" in (flat[0].text or "")
    assert flat[0].style == "primary"


def test_keyboard_contacts_by_button_fallback():
    """Кнопочный режим (контакта нет): единственная кнопка — «Показать
    контакты» с callback cnt:<job_id> (контакт даст 1 auth-запрос по нажатию)."""
    from app.parsing import Vacancy

    v = Vacancy(job_id=777, slug="x", url="https://hirify.me/jobs/777-x")
    v.contacts_by_button = True
    kb = inline_keyboard(v)
    flat = [b for row in kb.inline_keyboard for b in row]
    assert len(flat) == 1
    btn = flat[0]
    assert "Показать контакты" in (btn.text or "")
    assert btn.callback_data == "cnt:777"
    assert btn.url is None and btn.copy_text is None


def test_keyboard_no_button_for_autocard_without_contacts():
    """Обычная вакансия без контактов (НЕ кнопочный режим — например,
    автокарточка из своего текста): кнопки не появляется вовсе."""
    from app.parsing import Vacancy

    v = Vacancy(job_id=1, slug="x", url="https://hirify.me/jobs/1-x")
    kb = inline_keyboard(v)
    assert not kb.inline_keyboard


def test_no_source_label_in_manual_and_channel_cards():
    """Решение 13.11: карточка из своего текста не подписывается «из вашего
    текста», карточка из ТГ-канала не называет канал-источник — в шапке
    остаётся только компания (разделы hirify свои подписи сохраняют)."""
    from app.textparse import parse_vacancy_text

    text = ("Ищем Senior Rust Developer в компанию Vega Protocol, remote,\n"
            "$3000-5000. Стек: Rust, Solana. Писать: @hr_vega")
    # 1) свой текст (дефолт parse_vacancy_text)
    v = parse_vacancy_text(text)
    assert v.feed_key == "manual" and v.feed_label == ""
    for html in (render_rich_html(v), render_classic_html(v)):
        assert "из вашего текста" not in html
        assert "Vega Protocol" in html

    # 2) пост из канала: feed_key маршрутизируется, метка пуста
    v2 = parse_vacancy_text(text, feed_key="tg_cpa_people", feed_label="")
    assert v2.feed_key == "tg_cpa_people" and v2.feed_label == ""
    for html in (render_rich_html(v2), render_classic_html(v2)):
        assert "Cpa People" not in html and "cpa_people" not in html

    # 3) раздел hirify подпись сохраняет (категория, не источник)
    v3 = parse_vacancy_text(text, feed_key="remote", feed_label="🏠 Удалёнка")
    assert "Удалёнка" in render_rich_html(v3)
