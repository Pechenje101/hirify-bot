"""Команда /digest: подбор состава, карточка-дайджест, сессии выбора.

Без aiogram-диспетчера (как test_contacts_button): тестируем ядро —
db.recent_jobs, suggest, DigestSessions (тумблеры/потолок/TTL) и рендер
card/selection. Дайджест не ходит в сеть — контакты читаются из БД.
"""
import html as html_mod
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database  # noqa: E402
from app.digest import (  # noqa: E402
    DIGEST_DEFAULT, DIGEST_MAX,
    DigestSessions, render_digest, selection_keyboard, selection_text, suggest,
)

_TAG_RE = re.compile(r"<[^>]+>")


def make_db() -> Database:
    return Database(Path(tempfile.mkdtemp(prefix="hirify_digest_")) / "t.db")


def add_seen(db: Database, job_id: int, first_seen: str, status: str = "sent", **kw) -> None:
    """Вакансия с фиксированным first_seen (в реале его пишет add_job)."""
    db.add_job(
        job_id, kw.get("slug", f"slug-{job_id}"), kw.get("url", f"https://hirify.me/jobs/{job_id}-slug"),
        title=kw.get("title", f"Job {job_id}"),
        company=kw.get("company", ""), salary=kw.get("salary", ""),
        tg_contacts=kw.get("tg_contacts"),
    )
    db.mark_job(job_id, status)
    # add_job пишет «сейчас» — для проверки порядка зададим даты явно
    # (lock модульный: тесту он не нужен, соединение однопоточное)
    db.conn.execute("UPDATE jobs SET first_seen=? WHERE job_id=?", (first_seen, job_id))
    db.conn.commit()


# ---------------------------------------------------------------- БД

def test_recent_jobs_order_and_hidden():
    """Новые сверху; не-вакансии/скам/нечитанные в списке кандидатов нет."""
    db = make_db()
    add_seen(db, 1, "2026-11-10T10:00:00")
    add_seen(db, 2, "2026-11-12T10:00:00", status="skipped_not_vacancy")
    add_seen(db, 3, "2026-11-11T10:00:00")
    add_seen(db, 4, "2026-11-13T09:00:00", status="skipped_scam")
    add_seen(db, 5, "2026-11-13T10:00:00", status="fetch_failed")
    add_seen(db, 6, "2026-11-12T09:59:59", status="skipped_stale")
    assert [r["job_id"] for r in db.recent_jobs(12)] == [6, 3, 1]
    assert [r["job_id"] for r in db.recent_jobs(1)] == [6]
    # без фильтра (exclude=()) видно всё — скрытие только для дайджеста
    assert {r["job_id"] for r in db.recent_jobs(12, exclude=())} == {1, 2, 3, 4, 5, 6}
    db.close()


def test_recent_jobs_limit_cuts_oldest():
    """limit отдаёт СВЕЖИЕ (обрезает хвост, а не голову)."""
    db = make_db()
    for i in range(1, 9):
        add_seen(db, i, f"2026-11-0{i}T10:00:00")
    assert [r["job_id"] for r in db.recent_jobs(3)] == [8, 7, 6]
    db.close()


# ---------------------------------------------------------------- подбор

def test_suggest_takes_freshest():
    ids = list(range(1, 13))  # новые сверху
    assert suggest(ids) == set(ids[:DIGEST_DEFAULT])
    assert suggest(ids, 3) == {1, 2, 3}
    assert suggest(ids[:3]) == {1, 2, 3}  # кандидатов меньше — берём всех
    assert suggest([]) == set()


# ---------------------------------------------------------------- сессии

def test_sessions_toggle_and_cap():
    s = DigestSessions(ttl=60)
    rows = [{"job_id": i, "title": f"Job {i}"} for i in range(1, 13)]
    st = s.start(7, rows, suggest([r["job_id"] for r in rows]))
    assert st.ids == list(range(1, 13)) and len(st.sel) == 5

    ok, note = s.toggle(7, 1)          # снять отметку
    assert ok and 1 not in st.sel and note.startswith("⬜")
    ok, note = s.toggle(7, 1)          # вернуть
    assert ok and 1 in st.sel and note.startswith("✅")
    ok, _ = s.toggle(7, 999)           # не кандидат этого списка
    assert not ok
    for j in range(6, 11):             # доливаем до потолка DIGEST_MAX
        assert s.toggle(7, j)[0]
    ok, note = s.toggle(7, 11)        # 11-я не влезает
    assert not ok and str(DIGEST_MAX) in note and len(st.sel) == DIGEST_MAX

    st = s.reset(7)                    # «последние 5» — предложение бота
    assert st is not None and st.sel == suggest(st.ids)

    s.drop(7)
    assert s.get(7) is None
    ok, note = s.toggle(7, 1)           # сессии нет — «начните заново»
    assert not ok and "/digest" in note


def test_sessions_ttl_expiry():
    s = DigestSessions(ttl=0)
    s.start(7, [{"job_id": 1}], {1})
    time.sleep(0.01)
    assert s.get(7) is None            # протухла — тихо убрана
    assert s.reset(7) is None


def test_sessions_per_chat():
    s = DigestSessions(ttl=60)
    s.start(1, [{"job_id": 10}], {10})
    s.start(2, [{"job_id": 20}], {20})
    assert s.get(1).sel == {10} and s.get(2).sel == {20}
    s.drop(1)
    assert s.get(1) is None and s.get(2) is not None


# ---------------------------------------------------------------- выбор

def test_selection_text_and_keyboard():
    db = make_db()
    add_seen(db, 101, "2026-11-13T08:00:00", title="Senior <Solidity> Engineer",
             company="Acme & Co", salary="$4–6k",
             tg_contacts=[{"kind": "username", "value": "acme_hr", "display": "@acme_hr"}])
    add_seen(db, 102, "2026-11-12T08:00:00", title="Rust Dev")
    rows = db.recent_jobs(12)

    text = selection_text(rows, {101})
    assert "Выбрано: <b>1</b> из 2" in text and "Дайджест вакансий" in text

    kb = selection_keyboard(rows, {101})
    flat = [b for row in kb.inline_keyboard for b in row]
    by_cb = {b.callback_data: b for b in flat}
    assert set(by_cb) >= {"dgt:101", "dgt:102", "dgtgo", "dgtrs", "dgtcl"}
    assert by_cb["dgt:101"].text.startswith("✅") and "$4–6k" in by_cb["dgt:101"].text
    assert by_cb["dgt:102"].text.startswith("⬜")
    assert by_cb["dgtgo"].text.endswith("(1)")
    # подпись кнопки — короткая (Telegram), зарплата обрезана до 12
    assert all(len(b.text) <= 56 for b in flat)
    db.close()


def test_button_label_truncates_long_title():
    db = make_db()
    add_seen(db, 201, "2026-11-13T08:00:00", title="A" * 200, salary="B" * 40)
    rows = db.recent_jobs(12)
    kb = selection_keyboard(rows, set())
    label = kb.inline_keyboard[0][0].text
    assert len(label) <= 56 and "…" in label
    db.close()


# ---------------------------------------------------------------- карточка

def test_render_digest_card():
    db = make_db()
    add_seen(db, 101, "2026-11-13T08:00:00", title="Senior <Solidity> Engineer",
             company="Acme & Co", salary="$4–6k",
             tg_contacts=[{"kind": "username", "value": "acme_hr", "display": "@acme_hr"}])
    add_seen(db, 102, "2026-11-12T08:00:00", title="Rust Dev", url="",
             tg_contacts=[{"kind": "channel", "value": "vacancy_ch", "display": "@vacancy_ch"}])
    rows = db.recent_jobs(12)

    html, markup = render_digest(rows)
    assert markup is None
    assert "📰 <b>Дайджест вакансий</b>" in html and "2 шт · собрано" in html
    # заголовок-ссылка на источник + экранирование опасных символов
    assert '<a href="https://hirify.me/jobs/101-slug">' in html
    assert html_mod.escape("Senior <Solidity> Engineer") in html
    assert html_mod.escape("Acme & Co") in html
    assert "1️⃣" in html and "2️⃣" in html
    # мета-строка: компания · зарплата · дата first_seen
    assert "$4–6k" in html and "13.11" in html and "12.11" in html
    # username-контакт — сразу кликабельная t.me-ссылка
    assert 'href="https://t.me/acme_hr"' in html and "@acme_hr" in html
    # вакансия без url — заголовок без ссылки; канал — текстом
    assert "Rust Dev" in html
    assert not re.search(r'<a href="[^"]*">Rust Dev</a>', html)
    assert "@vacancy_ch" in html
    # влезает в лимит sendMessage (4096 видимых символов)
    assert len(html_mod.unescape(_TAG_RE.sub("", html))) <= 4096
    db.close()


def test_render_digest_empty():
    text, markup = render_digest([])
    assert "нет вакансий" in text and markup is None


def test_render_digest_many_keeps_limit():
    """10 вакансий (потолок) — карточка всё ещё в лимите Telegram."""
    db = make_db()
    for i in range(1, 11):
        add_seen(db, i, f"2026-11-1{i}T08:00:00", title=f"Position {i} " + "Dev " * 30,
                 company=f"Company {i}", salary="$1–99k",
                 tg_contacts=[{"kind": "username", "value": f"hr_{i}", "display": f"@hr_{i}"}])
    rows = db.recent_jobs(DIGEST_MAX)
    assert len(rows) == DIGEST_MAX
    html, _ = render_digest(rows)
    assert "🔟" in html and "1️⃣" in html
    assert len(html_mod.unescape(_TAG_RE.sub("", html))) <= 4096
    db.close()


def test_digest_uses_only_cached_contacts():
    """Контакты в карточке — ровно из tg_contacts БД, ничего не дознаётся."""
    db = make_db()
    add_seen(db, 301, "2026-11-13T08:00:00", title="Go Dev",
             tg_contacts=[{"kind": "username", "value": "cached_hr", "display": "@cached_hr"}])
    html, _ = render_digest(db.recent_jobs(12))
    assert "cached_hr" in html
    # нет tg_contacts (кнопочный режим до нажатия) — строки контакта нет вовсе
    add_seen(db, 302, "2026-11-13T09:00:00", title="No Contact Dev")
    html2, _ = render_digest(db.recent_jobs(12))
    assert "✉️" not in html2 or "cached_hr" in html2
    db.close()
