"""Рендер карточек вакансий — стилистика Apple + «панельная» глубина Telegram.

Дизайн-язык: чистая типографика без «зоопарка» эмодзи, ясная иерархия
(заголовок → компания и раздел → зарплата), факты — отдельной панелью,
контакт рекрутера — выделенной цитатой-панелью. Плюс интерактивность в рамках
того, что реально рендерит Telegram: сворачиваемое описание, сворачиваемый
список доп. контактов, кнопки-действия. Важно: CSS (и тени) в сообщениях
Telegram недоступны, поэтому «объём» даём панелями — современные клиенты
рисуют blockquote с фоном и акцентной полосой, а details — сворачиваемым.

Ритм rich-карточки: заголовок → компания · раздел → зарплата → таблица
фактов → разделитель <hr/> → описание (тизер + сворачиваемый блок) →
панель рекрутера → сворачиваемые доп. контакты → кнопки-действия →
мелкий футер <footer> («hirify.me · опубликовано сегодня/вчера/N дней назад»).
Тонкая линия <hr/> и приглушённый <footer> — поддерживаются Rich HTML
(Bot API 10.1–10.3) и дают «дыхание» композиции без CSS.

Кнопки: «Написать рекрутеру» — style=success (rich) / style=primary
(классика, Bot API 9.4+) — акцентная, «Копировать контакт» — нейтральная.

Тени/слои в полном объёме показывает HTML-превью (scripts/make_preview.py).

Два формата из одних данных:
1. Rich Message (Bot API 10.1+, поле html): h3-заголовок, компактная таблица
   фактов, тизер + сворачиваемое описание, панель контакта, tg-кнопки, футер.
2. Классический parse_mode=HTML + InlineKeyboardMarkup — работает у всех;
   факты и контакт тоже панелями-цитатами (blockquote поддерживается).
"""
from __future__ import annotations

import html as html_mod
import re
from datetime import date, datetime
from typing import Optional

from .parsing import TGContact, Vacancy

E = html_mod.escape

# Ведущие эмодзи из меток разделов («🏠 Удалёнка») убираем — в карточке
# стилистика Apple требует сдержанности; в /status метки остаются как есть.
_LEAD_NOISE_RE = re.compile(r"^[\W_]+", re.UNICODE)


def _clean_label(label: str) -> str:
    return _LEAD_NOISE_RE.sub("", label or "").strip()


# ---------------------------------------------------------------- мелочи

def _fmt_date(iso: str) -> str:
    """'2026-11-10T…' → '10.11.2026' (или как было, если не похоже на дату)."""
    if not iso:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso)
    if not m:
        return iso[:10]
    y, mo, d = m.groups()
    return f"{d}.{mo}.{y}"


def _fmt_freshness(date_posted: str, now: date | datetime | None = None) -> str:
    """Относительная свежесть: «опубликовано сегодня», «вчера», «4 дня назад»,
    а для более старых — точная дата DD.MM.YYYY (чисто и без лишнего шума).

    Дату берём из date_posted (обычно UTC): расхождение с локальной зоной
    в несколько часов у границ суток не критично, поэтому считаем
    по календарной дате UTC.
    """
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", (date_posted or "").strip())
    if not m:
        return ""
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return ""
    today = now or date.today()
    if isinstance(today, datetime):
        today = today.date()
    days = (today - d).days
    if days <= 0:
        return "опубликовано сегодня"
    if days == 1:
        return "опубликовано вчера"
    if days <= 4:
        return f"опубликовано {days} дня назад"
    if days <= 7:
        return f"опубликовано {days} дней назад"
    return f"опубликовано {d.strftime('%d.%m.%Y')}"


def _format_badge(v: Vacancy) -> str:
    """Каноничный формат работы: Global Remote / Remote / Hybrid / Onsite."""
    wf = (v.work_format or "").lower()
    if "global" in wf:
        return "Global Remote"
    if "remote" in wf or "удал" in wf or "дистанц" in wf:
        return "Remote"
    if "hybrid" in wf or "гибрид" in wf:
        return "Hybrid"
    if "onsite" in wf or "on-site" in wf or "офис" in wf:
        return "Onsite"
    return E(v.work_format) if v.work_format else ""


def _skills_line(v: Vacancy, max_skills: int = 7) -> str:
    if not v.skills:
        return ""
    return " · ".join(E(s) for s in v.skills[:max_skills])


def _teaser(text: str, maxlen: int = 220) -> str:
    """Первые 1–2 предложения описания — «крючок» до сворачиваемого блока."""
    text = (text or "").strip()
    if not text:
        return ""
    first = text.split("\n\n", 1)[0].strip()
    if len(first) <= maxlen:
        return first
    cut = first[:maxlen]
    dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if dot > maxlen // 2:
        cut = cut[: dot + 1]
    return cut.rstrip(" ,;:-") + "…"


def _contact_name(c: TGContact) -> str:
    """Как показать контакт в тексте карточки."""
    if c.kind == "user_id":
        return "рекрутер"  # имя скрыто — доступ по кнопке
    return c.display


def _description_paragraphs(text: str, limit: int) -> str:
    """Обрезаем описание до limit и возвращаем готовый текст (БЕЗ экранирования здесь)."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > limit:
        cut = text[:limit]
        # режем по последнему переносу, чтобы не рвать слово
        nl = cut.rfind("\n")
        if nl > limit // 2:
            cut = cut[:nl]
        text = cut.rstrip() + "\n…(полный текст — на hirify.me)"
    return text


def _head_parts(v: Vacancy) -> list[str]:
    """Шапка карточки: компания · раздел (без эмодзи)."""
    head: list[str] = []
    if v.company:
        head.append(f"<b>{E(v.company)}</b>")
    label = _clean_label(v.feed_label)
    if label:
        head.append(E(label))
    return head


def _facts_rows(v: Vacancy, include_employment: bool = True) -> list[tuple[str, str]]:
    """Строки фактов «подпись → значение» (общие для rich-таблицы и классики).

    Дату публикации в таблицу сознательно не кладём — она живёт в футере
    карточки как относительная свежесть («опубликовано сегодня/вчера/…»),
    не смешиваясь с фактами о работе.
    """
    rows: list[tuple[str, str]] = []
    badge = _format_badge(v)
    if badge:
        rows.append(("Формат", badge))
    if v.location:
        rows.append(("Локация", E(v.location)))
    if include_employment:
        emp = " · ".join(x for x in [v.employment_type, v.grade] if x)
        if emp:
            rows.append(("Занятость", E(emp)))
    skills = _skills_line(v)
    if skills:
        rows.append(("Стек", skills))
    return rows


def _footer_line(v: Vacancy) -> str:
    """Мелкая строка-подвал «hirify.me · опубликовано …» (без даты, если её нет)."""
    fresh = _fmt_freshness(v.date_posted)
    return "hirify.me · " + fresh if fresh else "hirify.me"


def _extra_contacts(v: Vacancy, cap: int = 5) -> list[TGContact]:
    """Личные TG-контакты помимо основного (для сворачиваемого списка)."""
    primary = v.primary_contact
    out: list[TGContact] = []
    seen: set[str] = set()
    for c in v.contacts or []:
        if not c.is_personal:
            continue
        if primary and c.kind == primary.kind and c.value.lower() == primary.value.lower():
            continue
        key = f"{c.kind}:{c.value.lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= cap:
            break
    return out


def _recruiter_panel_rich(primary: TGContact) -> str:
    """Панель контакта рекрутера (blockquote — подложка с акцентом в клиентах)."""
    if primary.kind == "user_id":
        return (
            "<blockquote>👤 <b>Прямой контакт рекрутера</b><br/>"
            "Кнопка ниже откроет чат в Telegram одним касанием</blockquote>"
        )
    return (
        f"<blockquote>👤 Рекрутер: <b>{E(_contact_name(primary))}</b><br/>"
        f"Написать — кнопка ниже, чат откроется сразу</blockquote>"
    )


# ---------------------------------------------------------------- rich html

def render_rich_html(v: Vacancy, limit: int = 1800) -> str:
    """HTML для InputRichMessage.html (Bot API 10.1+).

    Слои карточки (сверху вниз): заголовок → компания · раздел → зарплата →
    таблица-панель фактов → <hr/> → описание (тизер + сворачиваемый блок) →
    панель рекрутера → сворачиваемые доп. контакты → кнопки-действия →
    <footer> с источником и свежестью.
    """
    parts: list[str] = []

    # --- заголовок и контекст ---
    parts.append(f"<h3>{E(v.title or 'Вакансия')}</h3>")
    head = _head_parts(v)
    if head:
        parts.append("<p>" + " · ".join(head) + "</p>")

    # --- зарплата: отдельная жирная строка, как ценник ---
    if v.salary:
        parts.append(f"<p><b>{E(v.salary)}</b></p>")

    # --- таблица фактов: приглушённые подписи слева, значения справа ---
    rows = _facts_rows(v)
    if rows:
        tr = "".join(f"<tr><td>{lbl}</td><td>{val}</td></tr>" for lbl, val in rows)
        parts.append(f"<table compact>{tr}</table>")
        parts.append("<hr/>")  # тонкий разделитель: факты закончились, дальше — о работе

    # --- описание: тизер + сворачиваемый полный текст ---
    full = _description_paragraphs(v.description, limit)
    if full:
        teaser = _teaser(full)
        body = E(full).replace(chr(10), "<br/>")
        if teaser and teaser != full:
            parts.append(f"<p>{E(teaser)}</p>")
            if len(full) > len(teaser) + 80:
                parts.append(f"<details><summary>Показать полное описание</summary>{body}</details>")
        else:
            parts.append(f"<details><summary>Описание вакансии</summary>{body}</details>")

    # --- панель контакта рекрутера ---
    primary = v.primary_contact
    if primary:
        parts.append(_recruiter_panel_rich(primary))

    # --- дополнительные контакты (если нашлись) — сворачиваемая панель ---
    extra = _extra_contacts(v)
    if extra:
        lines = "<br/>".join(
            f'<a href="{E(c.url, quote=True)}">{E(c.display)}</a>' for c in extra
        )
        parts.append(
            f"<details><summary>Ещё Telegram-контакты: {len(extra)}</summary>{lines}</details>"
        )

    # --- кнопки: только действие — написать и скопировать контакт ---
    btns: list[str] = []
    if primary:
        btns.append(
            f'<tg-button type="url" style="success" url="{E(primary.url, quote=True)}">Написать рекрутеру</tg-button>'
        )
        if primary.kind == "username":
            btns.append(
                f'<tg-button type="copy_text" text="{E(primary.display, quote=True)}">Копировать контакт</tg-button>'
            )
    if btns:
        parts.append('<tg-button-row align="left">' + "".join(btns[:2]) + "</tg-button-row>")
        if len(btns) > 2:
            parts.append("<tg-button-row>" + "".join(btns[2:]) + "</tg-button-row>")

    # --- подвал: источник + относительная свежесть, мелким приглушённым ---
    parts.append(f"<footer>{E(_footer_line(v))}</footer>")

    return "\n".join(parts)


# ---------------------------------------------------------------- классика

def render_classic_html(v: Vacancy, limit: int = 1600) -> str:
    """HTML для sendMessage(parse_mode='HTML') — фолбэк для всех клиентов.

    Тоже «панельно»: факты и контакт — в blockquote (современные клиенты
    рисуют цитату с фоном и акцентной полосой), длинное описание —
    в сворачиваемой цитате (blockquote expandable).
    """
    lines: list[str] = []

    # шапка
    lines.append(f"<b>{E(v.title or 'Вакансия')}</b>")
    head = _head_parts(v)
    if head:
        lines.append(" · ".join(head))
    if v.salary:
        lines.append(f"<b>{E(v.salary)}</b>")

    # факты — панелью-цитатой
    rows = _facts_rows(v)
    if rows:
        lines.append(
            "<blockquote>" + "\n".join(f"{lbl} — {val}" for lbl, val in rows) + "</blockquote>"
        )

    # описание: тизер + expandable-цитата для полного текста
    full = _description_paragraphs(v.description, limit)
    if full:
        teaser = _teaser(full)
        esc = E(full)
        if teaser and teaser != full and len(full) > len(teaser) + 80:
            lines.append(E(teaser))
            lines.append("<blockquote expandable>" + esc + "</blockquote>")
        else:
            lines.append(esc)

    # контакт — тоже панелью
    primary = v.primary_contact
    if primary:
        if primary.kind == "username":
            panel = (
                f"👤 Рекрутер — <b>{E(primary.display)}</b> · "
                f'<a href="{E(primary.url, quote=True)}">написать в Telegram</a>'
            )
            more = _extra_contacts(v)
            if more:
                panel += "\nЕщё: " + " · ".join(
                    f'<a href="{E(c.url, quote=True)}">{E(c.display)}</a>' for c in more
                )
            lines.append("<blockquote>" + panel + "</blockquote>")
        else:
            lines.append(
                "<blockquote>👤 <b>Прямой контакт рекрутера</b> — кнопка ниже откроет чат в Telegram</blockquote>"
            )

    lines.append(f"<i>{E(_footer_line(v))}</i>")
    return "\n\n".join(lines)


# ---------------------------------------------------------------- клавиатура

def inline_keyboard(v: Vacancy):
    """InlineKeyboardMarkup для классического сообщения: максимум два действия."""
    from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

    buttons: list[list] = []
    primary = v.primary_contact
    if primary:
        row = [
            InlineKeyboardButton(
                text="✉️ Написать рекрутеру",
                url=primary.url,
                style="primary",  # Bot API 9.4+: акцентная синяя кнопка
            )
        ]
        if primary.kind == "username":
            row.append(
                InlineKeyboardButton(
                    text="📋 Копировать контакт",
                    copy_text=CopyTextButton(text=primary.display),
                )
            )
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------- демо

def demo_vacancy() -> Vacancy:
    """Синтетическая вакансия для команды /test (без обращения к hirify.me)."""
    v = Vacancy(
        job_id=999999,
        slug="demo-senior-solidity-engineer",
        url="https://hirify.me/jobs/999999-demo-senior-solidity-engineer",
        title="Senior Solidity Engineer (Demo)",
        company="ChainDemo Labs",
        salary="4 500 – 6 500 USD/мес",
        work_format="Global Remote",
        employment_type="Full-time",
        grade="Senior",
        location="из любой точки мира",
        skills=["solidity", "evm", "hardhat", "web3.js", "react", "postgres"],
        description=(
            "Так будет выглядеть вакансия из раздела «Глобальная удалёнка» "
            "с прямым Telegram-контактом рекрутера — карточка в стилистике Apple: "
            "чистая типографика, таблица фактов, ничего лишнего.\n\n"
            "Что делает бот:\n"
            "1. Следит за разделами hirify.me: Удалёнка, Глобальная удалёнка и Crypto & Web3\n"
            "2. Берёт только вакансии с фильтром only_direct_contact=1&contact_types=telegram\n"
            "3. Присылает карточку сразу, как только находит подходящую вакансию\n\n"
            "Стек: EVM-сети, смарт-контракты, DeFi-протоколы. Полностью remote — "
            "работаем из любой точки мира, зарплата в USDT.\n\n"
            "Это демо-карточка — данные не настоящие."
        ),
        date_posted="2026-11-11",
        source_type="telegram",
        feed_key="global",
        feed_label="🌍 Глобальная удалёнка",
        contacts=[
            TGContact("username", "demo_recruiter", "@demo_recruiter",
                      "https://t.me/demo_recruiter", "contacts"),
            TGContact("username", "chaindemo_hr", "@chaindemo_hr",
                      "https://t.me/chaindemo_hr", "body"),
        ],
    )
    return v
