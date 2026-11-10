"""Bubble-карточка вакансии: HTML-виджет для sendWidget (Bot API 10.4, Telegram 13.2+).

Здесь, в отличие от Rich Messages, наконец доступен настоящий CSS: тени,
скругления, панели — та самая «карточка как в самой новой версии Telegram».
Правила из официального гайда (core.telegram.org/bots/widgets), которые мы соблюдаем:

- палитра — только CSS-переменные клиента (--tg-theme-*), светлая/тёмная тема
  подхватывается автоматически; никакой жёстко заданной «контрастной» расцветки;
- шрифты — только системные (system-ui), размеры в em от текста чата;
- ≤16 «тяжёлых» элементов с blur-тенью — у нас их 3 (панель фактов,
  панель рекрутера, чип зарплаты), так что запас большой;
- высота виджета авто (50–600 px) и НЕ скроллится — поэтому описание даём
  тизером (2–4 строки), а полный текст живёт в rich/классике и на сайте;
- действия — нативные <button>: openTelegramLink('https://t.me/…') открывает
  чат с рекрутером без подтверждений, copyText копирует @юзернейм с тостом;
  для контакта вида tg://user?id= кнопок внутри нет (виджет умеет только
  https://t.me) — вместо них под виджет уходит обычная инлайн-клавиатура
  (её собирает sender.py через inline_keyboard());
- лимиты размера: 32768 байт сырого HTML и 4096 байт после zlib-9 —
  render_bubble_html проверяет оба и при переполнении ужимает описание;
- caption обязателен: это текст в уведомлениях, в поиске и то, что видят
  клиенты без поддержки виджетов.
"""
from __future__ import annotations

import html as html_mod
import zlib

from .cards import _clean_label, _contact_name, _extra_contacts, _facts_rows, _footer_line, _format_badge, _teaser
from .parsing import Vacancy

# Ограничения Bot API (с запасом)
MAX_RAW_BYTES = 32_768
MAX_COMPRESSED_BYTES = 4_096
CAPTION_MAX_CHARS = 1024

# Тизер описания в пузыре: высота виджета не скроллится, длинные тексты не влезают
BUBBLE_DESCRIPTION_LIMIT = 280

# Мини-CSS карточки. Палитра — из темы чата, глубина — настоящими тенями.
_BUBBLE_CSS = (
    "<style>"
    ".jc{font-family:system-ui,sans-serif}"
    ".jc .hd{color:var(--tg-theme-hint-color);font-size:.85em;font-weight:600}"
    ".jc h3{margin:.12em 0 .3em;font-size:1.24em;letter-spacing:-.01em}"
    ".jc .sal{display:inline-block;background:var(--tg-theme-secondary-bg-color);"
    "color:var(--tg-theme-accent-text-color);font-weight:700;font-size:.95em;"
    "border-radius:9px;padding:3px 10px;margin:1px 0 7px;"
    "box-shadow:0 1px 3px rgba(0,0,0,.09),0 4px 12px rgba(0,0,0,.07)}"
    ".jc .fx{background:var(--tg-theme-secondary-bg-color);border-radius:10px;"
    "padding:2px 11px;margin:4px 0 2px;"
    "box-shadow:0 1px 3px rgba(0,0,0,.08),0 4px 12px rgba(0,0,0,.06)}"
    ".jc .r{display:flex;justify-content:space-between;gap:12px;padding:4px 0;"
    "border-bottom:1px solid var(--tg-theme-section-separator-color);font-size:.92em}"
    ".jc .r:last-child{border-bottom:none}"
    ".jc .r span{color:var(--tg-theme-hint-color)}"
    ".jc .r b{text-align:right}"
    ".jc .ds{margin:8px 0 2px;font-size:.95em;white-space:pre-line;overflow-wrap:anywhere}"
    ".jc .pn{background:var(--tg-theme-secondary-bg-color);"
    "border-left:3px solid var(--tg-theme-link-color);border-radius:6px 10px 10px 6px;"
    "padding:7px 11px;margin:9px 0 2px;"
    "box-shadow:0 1px 3px rgba(0,0,0,.08),0 4px 12px rgba(0,0,0,.06)}"
    ".jc .pn .t{font-size:.87em;color:var(--tg-theme-hint-color)}"
    ".jc .pn .v{font-weight:700;user-select:text;-webkit-user-select:text}"
    ".jc .ex{margin-top:6px;font-size:.84em;color:var(--tg-theme-hint-color);"
    "user-select:text;-webkit-user-select:text;overflow-wrap:anywhere}"
    ".jc .bs{display:flex;gap:8px;margin:11px 0 2px}"
    ".jc .bs .cta{flex:1.7 1 0}"
    ".jc .bs .cp{flex:1 1 0;background:var(--tg-theme-secondary-bg-color);"
    "color:var(--tg-theme-text-color)}"
    ".jc .ft{margin-top:9px;font-size:.78em;color:var(--tg-theme-hint-color)}"
    "</style>"
)


def _esc(text: str, quote: bool = False) -> str:
    """Экранирование для HTML-виджета (как в cards.py)."""
    return html_mod.escape(text or "", quote=quote)


def _js_arg(text: str) -> str:
    """Подстраховка для JS-строк внутри onclick: @username и t.me-URL не
    содержат кавычек по правилам Telegram, но на всякий случай выкидываем
    символы, способные разорвать строку в обработчике."""
    return (text or "").replace("\\", "").replace("'", "").replace('"', "")


def _head_line(v: Vacancy) -> str:
    head: list[str] = []
    if v.company:
        head.append(_esc(v.company))
    label = _clean_label(v.feed_label)
    if label:
        head.append(_esc(label))
    return " · ".join(head)


def _buttons_html(v: Vacancy) -> str:
    """Кнопки-действия внутри виджета. Только для @username-контактов:
    openTelegramLink понимает лишь https://t.me/…, а tg://user?id= — нет,
    такие контакты открывает инлайн-клавиатура под виджетом (sender.py)."""
    primary = v.primary_contact
    if not primary or primary.kind != "username":
        return ""
    url = _js_arg(primary.url)
    handle = _js_arg(primary.display)
    btns = [
        f'<button class="cta" onclick="TelegramWidget.openTelegramLink(\'{url}\')">'
        f"✉️ Написать рекрутеру</button>",
        f'<button class="cp" onclick="TelegramWidget.copyText(\'{handle}\')">'
        f"📋 Копировать контакт</button>",
    ]
    return '<div class="bs">' + "".join(btns) + "</div>"


def _recruiter_panel(v: Vacancy) -> str:
    primary = v.primary_contact
    if not primary:
        return ""
    if primary.kind == "user_id":
        title, value = "Прямой контакт рекрутера", "откроется кнопкой под карточкой"
    else:
        title, value = "Рекрутер", _contact_name(primary)
    extra = _extra_contacts(v)
    ex = ""
    if extra:
        # Ссылки t.me внутри виджета не открываются (только через кнопки),
        # поэтому доп. контакты — просто выделяемый текст.
        names = ", ".join(_esc(c.display) for c in extra)
        ex = f'<div class="ex">Ещё в Telegram: {names}</div>'
    return (
        '<div class="pn">'
        f'<div class="t">👤 {_esc(title)}</div>'
        f'<div class="v">{_esc(value)}</div>'
        "</div>" + ex
    )


def _widget_size(html: str) -> tuple[int, int]:
    """(сырые байты, сжатые zlib-9 байты) — как их считает сервер Telegram."""
    raw = html.encode("utf-8")
    return len(raw), len(zlib.compress(raw, 9))


def render_bubble_html(v: Vacancy, limit: int = BUBBLE_DESCRIPTION_LIMIT) -> str:
    """HTML для sendWidget(html=…): пузырь с настоящими тенями и панелями.

    Слои: подзаголовок (компания · раздел) → заголовок → чип зарплаты →
    панель фактов → описание-тизер → панель рекрутера → кнопки-действия →
    футер «hirify.me · опубликовано …». Гарантированно влезает в лимиты
    Bot API (32 КБ / 4096 байт zlib-9): при переполнении ужимаем описание.
    """
    parts: list[str] = [_BUBBLE_CSS, '<div class="jc">']

    head = _head_line(v)
    if head:
        parts.append(f'<div class="hd">{head}</div>')
    parts.append(f"<h3>{_esc(v.title or 'Вакансия')}</h3>")

    if v.salary:
        parts.append(f'<div class="sal">{_esc(v.salary)}</div>')

    badge = _format_badge(v)
    rows = _facts_rows(v)
    if rows:
        tr = "".join(
            f'<div class="r"><span>{lbl}</span><b>{val}</b></div>' for lbl, val in rows
        )
        parts.append(f'<div class="fx">{tr}</div>')

    desc = (v.description or "").strip()
    if desc and limit > 0:
        teaser = _teaser(desc, maxlen=limit)
        if teaser:
            note = "" if len(desc) <= len(teaser) + 20 else " …(полное описание — на hirify.me)"
            parts.append(f'<div class="ds">{_esc(teaser)}{note}</div>')

    parts.append(_recruiter_panel(v))
    parts.append(_buttons_html(v))
    parts.append(f'<div class="ft">{_esc(_footer_line(v))}</div>')
    parts.append("</div>")

    html = "".join(parts)
    raw, comp = _widget_size(html)
    if comp <= MAX_COMPRESSED_BYTES and raw <= MAX_RAW_BYTES:
        return html

    # Описание — единственная переменная часть: жмём его, пока не влезет
    for step_limit in (limit, 180, 120, 60, 0):
        if step_limit == limit:
            continue
        trimmed = render_bubble_html(v, limit=step_limit)
        raw, comp = _widget_size(trimmed)
        if comp <= MAX_COMPRESSED_BYTES and raw <= MAX_RAW_BYTES:
            return trimmed
    # Последний рубеж: совсем без описания (CSS+шапка+контакт влезают всегда)
    return render_bubble_html(v, limit=0)


def render_bubble_caption(v: Vacancy) -> str:
    """Caption виджета (0–1024 символов, обычный текст): заголовок, компания,
    зарплата. Видно в списке чатов, в уведомлениях и в клиентах без
    поддержки виджетов — поэтому всегда что-то осмысленное."""
    bits = [b.strip() for b in (v.title, v.company, v.salary) if b and b.strip()]
    if not bits:
        bits = ["Вакансия на hirify.me"]
    out = " · ".join(bits)
    if len(out) > 200:
        out = out[:197].rstrip() + "…"
    return out


def assert_widget_fits(html: str) -> tuple[int, int]:
    """Проверка лимитов Bot API 10.4; возвращает (сырые, сжатые) байты.

    Используется в тестах и sender.py перед отправкой — сервер отклонит
    html ошибкой INPUT_MEDIA_WIDGET_TOO_LARGE, лучше поймать это заранее.
    """
    raw, comp = _widget_size(html)
    if comp > MAX_COMPRESSED_BYTES or raw > MAX_RAW_BYTES:
        raise ValueError(
            f"widget HTML не влезает в лимиты Bot API: raw={raw}/{MAX_RAW_BYTES}, "
            f"zlib-9={comp}/{MAX_COMPRESSED_BYTES}"
        )
    return raw, comp
