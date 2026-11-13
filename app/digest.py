"""Команда /digest — дайджест последних вакансий одной красивой карточкой.

Как работает:
1) /digest [N] — бот сам предлагает состав: свежие вакансии из БД (до 12
   кандидатов), из них предвыбраны последние N (по умолчанию 5);
2) кнопками ✅/⬜️ под сообщением набор правится (добавить/убрать вакансию,
   потолок — 10), «↩️ Последние 5» возвращает предложение бота;
3) «Составить дайджест» — печатается карточка-дайджест: номер, название
   (ссылка на источник), компания · зарплата · дата, контакт.

Важно про контакты: дайджест показывает только УЖЕ материализованные в БД
контакты (рассылка в auto-режиме или кнопка «Показать контакты»). Новых
auth-запросов к сайту дайджест НЕ делает — никаких просмотров аккаунта,
квот и риска флага: это просто другой формат уже известных данных.

Состояние выбора живёт в памяти процесса (30 мин) — как анти-даблклик кнопки
контактов: раны GitHub Actions живут ~55 мин, дольше сессия выбора и не нужна.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

E = html_mod.escape

# Сколько предвыбрать по умолчанию («предложение бота»)…
DIGEST_DEFAULT = 5
# …потолок выбранных (карточка остаётся читаемой, лимит 4096 не тревожим)
DIGEST_MAX = 10
# Сколько кандидатов показать кнопками
DIGEST_CANDIDATES = 12
# Сессия выбора живёт столько; тухлая — «вызовите /digest заново»
DIGEST_TTL_SEC = 30 * 60

# Номера-эмодзи для карточки (DIGEST_MAX штук)
_BADGES = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")

# Сколько символов вакансии катит в подписи кнопки выбора и в карточке
# (⬜️ — 2 кодпоинта: подпись из 36 заголовка + 12 зарплаты = ≤56 кодпоинтов)
_BUTTON_TITLE_MAX = 36
_CARD_TITLE_MAX = 120

_MSK = timezone(timedelta(hours=3))
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


# ---------------------------------------------------------------- состояние

@dataclass
class DigestState:
    """Сессия выбора дайджеста в одном чате: кандидаты и отметки."""
    rows: list[dict]                    # строки-кандидаты (новые сверху) — снапшот
    sel: set[int] = field(default_factory=set)
    ts: float = 0.0                     # последнее касание (monotonic)

    @property
    def ids(self) -> list[int]:
        return [int(r["job_id"]) for r in self.rows]


class DigestSessions:
    """Хранилище сессий /digest по чатам с TTL (потокобезопасное)."""

    def __init__(self, ttl: float = DIGEST_TTL_SEC):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._by_chat: dict[int, DigestState] = {}

    def start(self, chat_id: int, rows: list[dict], sel: set[int]) -> DigestState:
        st = DigestState(rows=list(rows), sel=set(sel), ts=time.monotonic())
        with self._lock:
            self._by_chat[chat_id] = st
        return st

    def get(self, chat_id: int) -> DigestState | None:
        with self._lock:
            st = self._by_chat.get(chat_id)
            if st is None:
                return None
            if time.monotonic() - st.ts > self._ttl:
                del self._by_chat[chat_id]  # протухла — тихо убираем
                return None
            st.ts = time.monotonic()
            return st

    def drop(self, chat_id: int) -> None:
        with self._lock:
            self._by_chat.pop(chat_id, None)

    def toggle(self, chat_id: int, job_id: int) -> tuple[bool, str]:
        """Отметить/снять вакансию. (ok, заметка для тоста callback)."""
        st = self.get(chat_id)
        if st is None:
            return False, "Сессия устарела — вызовите /digest заново"
        if job_id not in st.ids:
            return False, "Вакансия не из этого списка — обновите /digest"
        if job_id not in st.sel and len(st.sel) >= DIGEST_MAX:
            return False, f"В дайджест не больше {DIGEST_MAX} вакансий"
        if job_id in st.sel:
            st.sel.discard(job_id)
            return True, "⬜ Убрал из дайджеста"
        st.sel.add(job_id)
        return True, "✅ В дайджесте"

    def reset(self, chat_id: int) -> DigestState | None:
        """Вернуть предложение бота: последние DIGEST_DEFAULT."""
        st = self.get(chat_id)
        if st is None:
            return None
        st.sel = suggest(st.ids)
        st.ts = time.monotonic()
        return st

sessions = DigestSessions()


# --------------------------------------------------------------- подбор

def suggest(ids: list[int], want: int = DIGEST_DEFAULT) -> set[int]:
    """Предложение бота: предвыбрать последние `want` кандидатов (список
    отсортирован новыми сверху). Меньше кандидатов, чем want, — берём все."""
    return set(ids[:max(0, want)])


# ---------------------------------------------------------------- тексты

def _short_title(title: str, limit: int) -> str:
    t = " ".join((title or "").split())
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t


def _short_date(iso: str) -> str:
    """'2026-11-13T…' → '13.11' (или пусто, если не дата)."""
    m = _DATE_RE.match((iso or "").strip())
    return f"{m.group(3)}.{m.group(2)}" if m else ""


def _button_label(row: dict, on: bool) -> str:
    mark = "✅" if on else "⬜️"
    fallback = f"Вакансия #{row.get('job_id')}"
    t = _short_title(str(row.get("title") or fallback), _BUTTON_TITLE_MAX)
    salary = str(row.get("salary") or "").strip()
    if salary:
        return f"{mark} {t} · {salary[:12]}"
    return f"{mark} {t}"


def selection_text(rows: list[dict], sel: set[int]) -> str:
    k = len(sel)
    return (
        "📰 <b>Дайджест вакансий</b>\n\n"
        "Предложил свежие вакансии — они отмечены ✅. Кнопками ниже добавьте "
        "или уберите вакансии, затем «Составить дайджест».\n\n"
        f"Выбрано: <b>{k}</b> из {len(rows)}"
    )


def selection_keyboard(rows: list[dict], sel: set[int]) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    for r in rows:
        jid = int(r["job_id"])
        btns.append([InlineKeyboardButton(
            text=_button_label(r, jid in sel),
            callback_data=f"dgt:{jid}",
        )])
    k = len(sel)
    btns.append([InlineKeyboardButton(
        text=f"📝 Составить дайджест ({k})",
        callback_data="dgtgo",
    )])
    btns.append([
        InlineKeyboardButton(text=f"↩️ Последние {DIGEST_DEFAULT}", callback_data="dgtrs"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="dgtcl"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=btns)


# ------------------------------------------------------- карточка-дайджест

def _contacts_of(row: dict) -> list[dict]:
    try:
        data = json.loads(row.get("tg_contacts") or "[]")
    except (TypeError, ValueError):
        return []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def _contact_line(row: dict) -> str:
    """Контакты записи: до двух, username — сразу ссылкой t.me."""
    parts: list[str] = []
    for c in _contacts_of(row)[:2]:
        kind, value = str(c.get("kind") or ""), str(c.get("value") or "")
        display = str(c.get("display") or value or "").strip()
        if not display:
            continue
        if kind == "username" and value:
            uname = value.lstrip("@")
            parts.append(f'✉️ <a href="https://t.me/{E(uname)}">{E("@" + uname)}</a>')
        else:
            parts.append(f"✉️ {E(display)}")
    return "\n".join(parts)


def render_digest(rows: list[dict]) -> tuple[str, InlineKeyboardMarkup | None]:
    """Карточка-дайджест: (html, клавиатура|None).

    Каждая запись: номер-эмодзи → название-ссылка на источник → строка
    «компания · зарплата · дата» → контакт (если уже материализован в БД).
    Даты — first_seen (когда бот увидел вакансию), время сбора — МСК.
    """
    if not rows:
        return ("🙂 В базе пока нет вакансий для дайджеста — попробуйте позже "
                "(после первых находок: /fresh или автоматический проход).", None)

    now = datetime.now(_MSK)
    lines: list[str] = [
        "📰 <b>Дайджест вакансий</b>",
        f"<i>{len(rows)} шт · собрано {now.strftime('%d.%m, %H:%M')} МСК</i>",
    ]
    for i, r in enumerate(rows):
        jid, url = int(r["job_id"]), str(r.get("url") or "").strip()
        title = _short_title(str(r.get("title") or f"Вакансия #{jid}"), _CARD_TITLE_MAX)
        head = f'{_BADGES[i]} <a href="{E(url)}">{E(title)}</a>' if url else f"{_BADGES[i]} {E(title)}"
        meta = " · ".join(x for x in (
            _short_title(str(r.get("company") or ""), _CARD_TITLE_MAX),
            str(r.get("salary") or "").strip(),
            _short_date(str(r.get("first_seen") or "")),
        ) if x)
        lines.append("\n" + head)
        if meta:
            lines.append(f"<i>{E(meta)}</i>")
        c = _contact_line(r)
        if c:
            lines.append(c)
    lines.append("\n—\n<i>📰 /digest — собрать новый дайджест в любой момент</i>")
    return "\n".join(lines), None
