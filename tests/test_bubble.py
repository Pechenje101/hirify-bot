"""Тесты bubble-карточки: HTML-виджет для sendWidget (Bot API 10.4).

Проверяем структуру, экранирование, кнопки-действия и главное — гарантии
размера: 32768 байт сырого HTML и 4096 байт после zlib-9, иначе Telegram
отклонит отправку с INPUT_MEDIA_WIDGET_TOO_LARGE.
"""
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bubble import (  # noqa: E402
    BUBBLE_DESCRIPTION_LIMIT,
    MAX_COMPRESSED_BYTES,
    MAX_RAW_BYTES,
    assert_widget_fits,
    render_bubble_caption,
    render_bubble_html,
)
from app.cards import demo_vacancy  # noqa: E402

DEMO = demo_vacancy()


def test_bubble_structure():
    html = render_bubble_html(DEMO)
    assert html.startswith("<style>")           # CSS первым элементом
    assert '<div class="jc">' in html
    # шапка и заголовок
    assert "ChainDemo Labs · Глобальная удалёнка" in html
    assert "<h3>Senior Solidity Engineer (Demo)</h3>" in html
    # чип зарплаты
    assert '<div class="sal">4 500 – 6 500 USD/мес</div>' in html
    # панель фактов
    assert '<span>Формат</span><b>Global Remote</b>' in html
    assert '<span>Стек</span><b>solidity' in html
    # панель рекрутера + доп. контакты выделяемым текстом (без ссылок t.me)
    assert "👤 Рекрутер" in html and "@demo_recruiter" in html
    assert "Ещё в Telegram: @chaindemo_hr" in html
    assert 'href="https://t.me/chaindemo_hr"' not in html
    # футер со свежестью — только свежесть, без упоминания источника
    assert "опубликовано " in html
    assert "hirify" not in html.lower()


def test_bubble_buttons_inside_widget():
    """Для @username — кнопки с openTelegramLink/copyText внутри виджета."""
    html = render_bubble_html(DEMO)
    assert "TelegramWidget.openTelegramLink('https://t.me/demo_recruiter')" in html
    assert "TelegramWidget.copyText('@demo_recruiter')" in html
    assert "Написать рекрутеру" in html and "Копировать контакт" in html


def test_bubble_userid_contact_no_inner_buttons():
    """tg://user?id= виджет открыть не может — кнопок внутри нет, их даст
    инлайн-клавиатура под виджетом (sender.py)."""
    from app.parsing import TGContact

    v = demo_vacancy()
    v.contacts = [TGContact("user_id", "123456789", "tg://user?id=123456789",
                            "tg://user?id=123456789", "contacts")]
    html = render_bubble_html(v)
    assert "TelegramWidget.openTelegramLink" not in html
    assert "TelegramWidget.copyText" not in html
    assert "Прямой контакт рекрутера" in html
    assert "кнопкой под карточкой" in html


def test_bubble_uses_theme_variables_no_hardcoded_colors():
    """Палитра — из темы чата (--tg-theme-*), светлая/тёмная подхватится сама."""
    html = render_bubble_html(DEMO)
    assert "var(--tg-theme-text-color)" in html
    assert "var(--tg-theme-hint-color)" in html
    assert "var(--tg-theme-secondary-bg-color)" in html
    assert "var(--tg-theme-link-color)" in html
    # настоящие тени (то, чего не было в Rich Messages)
    assert "box-shadow" in html
    # шрифты только системные
    assert "system-ui" in html
    for banned in ("@font-face", "@import", "oklch(", "position:sticky", "text-shadow"):
        assert banned not in html


def test_bubble_fits_widget_limits():
    """Главная гарантия: сырой HTML ≤32 КБ и zlib-9 ≤4096 байт."""
    for vac in (DEMO, _vac_with_huge_description(20_000), _vac_with_wide_unicode()):
        html = render_bubble_html(vac)
        raw, comp = assert_widget_fits(html)  # бросит ValueError, если не влезает
        assert raw <= MAX_RAW_BYTES
        assert comp <= MAX_COMPRESSED_BYTES
        # перепроверка независимым способом
        assert len(zlib.compress(html.encode("utf-8"), 9)) == comp


def test_bubble_trims_description_but_keeps_structure():
    """Очень длинное описание ужимается, но карточка остаётся целой:
    тизер с многоточием, никаких заглушек про «полное описание на сайте»."""
    v = _vac_with_huge_description(50_000)
    html = render_bubble_html(v)
    assert "hirify" not in html.lower()
    assert "полное описание — на" not in html.lower()
    assert " …" in html            # длинный текст — просто многоточие
    assert "<h3>Big Description Role</h3>" in html
    assert '<div class="ft">' not in html  # даты нет — футера нет вовсе
    raw, comp = assert_widget_fits(html)
    assert comp <= MAX_COMPRESSED_BYTES


def test_bubble_escapes_dangerous_text():
    from app.parsing import Vacancy

    v = Vacancy(
        job_id=1, slug="x", url="https://hirify.me/jobs/1-x",
        title="Senior <script>alert(1)</script> Dev",
        company="Evil & Co <b>",
        description="Опыт с <b>HTML</b> и &символами обязателен",
    )
    html = render_bubble_html(v)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "Evil &amp; Co" in html


def test_bubble_caption():
    cap = render_bubble_caption(DEMO)
    assert cap == "Senior Solidity Engineer (Demo) · ChainDemo Labs · 4 500 – 6 500 USD/мес"
    # без полей — осмысленный минимум; длинное — обрезается
    from app.parsing import Vacancy

    assert render_bubble_caption(Vacancy(job_id=2, slug="s", url="u")) == "Вакансия"
    long_v = Vacancy(job_id=3, slug="s", url="u", title="Т" * 500)
    assert len(render_bubble_caption(long_v)) <= 200
    assert render_bubble_caption(long_v).endswith("…")


def test_bubble_teaser_limit_is_short():
    """Высота виджета не скроллится (50–600 px) — описание только тизером."""
    assert BUBBLE_DESCRIPTION_LIMIT <= 400


def test_bubble_minimal_vacancy():
    """Минимальная вакансия (только заголовок) — без пустых блоков и в лимитах."""
    from app.parsing import Vacancy

    v = Vacancy(job_id=9, slug="m", url="u", title="Just a title")
    html = render_bubble_html(v)
    assert '<div class="sal">' not in html
    assert '<div class="fx">' not in html
    assert '<div class="pn">' not in html
    assert '<div class="bs">' not in html
    assert '<div class="ft">' not in html  # без даты — без футера, пустых блоков нет
    assert "hirify" not in html.lower()
    assert_widget_fits(html)


def _vac_with_huge_description(length: int):
    from app.parsing import TGContact, Vacancy

    return Vacancy(
        job_id=10, slug="big", url="https://hirify.me/jobs/10-big",
        title="Big Description Role",
        company="Mega Corp",
        description=("очень длинный абзац про технологии и процессы\n\n" * (length // 50 + 1)),
        contacts=[TGContact("username", "big_hr", "@big_hr", "https://t.me/big_hr", "contacts")],
    )


def _vac_with_wide_unicode():
    """Кириллица и эмодзи сжимаются хуже ASCII — проверяем запас по zlib."""
    from app.parsing import TGContact, Vacancy

    return Vacancy(
        job_id=11, slug="ru", url="https://hirify.me/jobs/11-ru",
        title="🚀 Разработчик «Климов» — Ведущий 🛠️",
        company="ООО «Ромашка и К°»",
        salary="от 350 000 ₽ на руки",
        work_format="Удалённо (Россия)",
        location="Россия, удалённо 🏠",
        skills=["python", "django", "postgresql", "docker", "kubernetes", "redis", "celery"],
        description=("Мы ищем опытного разработчика. " * 40) + "🚀🔥💡",
        contacts=[TGContact("username", "ромашка_минус_hr", "@romashka_hr", "https://t.me/romashka_hr", "contacts")],
    )
