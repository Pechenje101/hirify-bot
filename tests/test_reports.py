"""Отчёты конвейера канала: вечерний дайджест и недельная аналитика.

maybe_scheduled вызывается из цикла монитора каждый проход: дешёвая проверка
маркеров в БД, отправка — одним сообщением, маркер двигается только после
успеха (раны Actions самозамещаются — маркеры обязаны жить в БД).
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.reports import (  # noqa: E402
    channel_post_link, maybe_scheduled, render_channel_digest,
    render_week_report,
)

ADMIN = 1
CHANNEL = -1001234


class FakeSender:
    def __init__(self):
        self.texts: list = []

        class _Bot:
            async def get_chat(self, chat_id):
                if chat_id == CHANNEL:
                    return SimpleNamespace(username="my_vac_chan")
                return SimpleNamespace(username=None)

        self.bot = _Bot()

    async def send_text(self, chat_id, html, markup=None):
        if getattr(self, "fail", False):
            raise RuntimeError("fl00d")
        self.texts.append((chat_id, html))

    async def send_vacancy(self, chat_id, v, **kw):
        return "classic"


def make_cfg(chan_hour=21, week_hour=20) -> Config:
    cfg = Config(bot_token="x")
    cfg.tg_channels = ""
    cfg.admin_chat_id = ADMIN
    cfg.chan_digest_hour = chan_hour
    cfg.week_report_hour = week_hour
    return cfg


def msk(hour=21, weekday=6):
    """Фиксированное «сразу» по МСК (8.11.2026 — воскресенье)."""
    return datetime(2026, 11, 8, hour, 5, tzinfo=timezone(timedelta(hours=3)))


def seed_posted(db, n=2):
    """n опубликованных постов за сутки."""
    for i in range(1, n + 1):
        db.add_job(1000 + i, f"s{i}", f"https://t.me/x/{i}",
                   title=f"Senior Engineer {i}" if i % 2 else "QA Lead",
                   company=f"ChainX {i}", salary="$4–6k")
        db.save_moderation(1000 + i, "{}")
        db.mark_posted(1000 + i, 50 + i)
    return db


# ---------------------------------------------------------------- рендеры

def test_render_channel_digest_links_and_escaping():
    db = seed_posted(Database(Path(tempfile.mkdtemp(prefix="hirify_rep_")) / "t.db"), 2)
    rows = db.posted_since("2000-01-01", 10)
    html = render_channel_digest(rows, "https://t.me/my_vac_chan", msk())
    assert "Вакансии дня" in html
    assert 'href="https://t.me/my_vac_chan/51"' in html  # пост 1 → msg 51
    assert "ChainX 1" in html and "$4–6k" in html
    assert html.count("<a href=") == 2
    # пустой день — честная заглушка
    empty = render_channel_digest([], "https://t.me/x", msk())
    assert "не было" in empty
    db.close()


def test_render_channel_digest_shows_total_over_limit_overflow():
    """Показываем максимум 10 позиций, но «Всего за день: N» честно говорит,
    сколько вышло постов (раньше rows обрезался SQL LIMIT'ом и хвост
    «Всего за день» никогда не рисовался)."""
    db = seed_posted(Database(Path(tempfile.mkdtemp(prefix="hirify_rep_")) / "t.db"), 12)
    rows = db.posted_since("2000-01-01", 10)   # SQL уже обрезал до 10
    total = db.posted_count_since("2000-01-01")
    assert total == 12
    html = render_channel_digest(rows, "https://t.me/x", msk(), total=total)
    assert "Всего за день: 12" in html
    assert html.count("<a href=") == 10       # в карточке — только первые 10
    # без явного total — старое поведение (хвост не рисуем)
    html2 = render_channel_digest(rows, "https://t.me/x", msk())
    assert "Всего за день" not in html2
    db.close()


def test_channel_post_link_internal_format():
    # приватный канал: t.me/c/<внутренний id>/<msg>
    assert channel_post_link("https://t.me/c/1234", 7) == "https://t.me/c/1234/7"


def test_render_week_report_counts():
    db = seed_posted(Database(Path(tempfile.mkdtemp(prefix="hirify_rep_")) / "t.db"), 3)
    db.mark_channel_post_gone(1002, "closed")  # один пост закрыт — считаем вышедшим
    db.add_job(2000, "s", "https://t.me/x/2000", title="В очереди")
    db.save_moderation(2000, "{}")  # висит в очереди
    html = render_week_report(db, msk())
    assert "Постов в канале: <b>3</b>" in html    # posted+closed за окно
    assert "откликов" not in html                 # воронки «Откликнуться» больше нет
    assert "в очереди 1" in html
    db.close()


# ---------------------------------------------------------------- расписание

def test_maybe_scheduled_sends_digest_once():
    db = seed_posted(Database(Path(tempfile.mkdtemp(prefix="hirify_rep_")) / "t.db"))
    db.set_setting("post_channel_id", str(CHANNEL))
    sender = FakeSender()
    cfg = make_cfg()

    import app.reports as R
    orig = R._msk_now
    R._msk_now = lambda: msk()
    try:
        asyncio.run(maybe_scheduled(cfg, db, sender))   # воскресенье 21:00
        assert len(sender.texts) == 2                   # дайджест + недельный отчёт
        dg = sender.texts[0]
        assert dg[0] == CHANNEL and "Вакансии дня" in dg[1]
        assert dg[1].count("t.me/my_vac_chan/") == 2    # ссылки на посты
        wk = sender.texts[1]
        assert wk[0] == ADMIN and "Неделя канала" in wk[1]
        # маркеры дня/недели проставлены — повторный вызов НЕ дублирует
        asyncio.run(maybe_scheduled(cfg, db, sender))
        assert len(sender.texts) == 2
    finally:
        R._msk_now = orig
    db.close()


def test_maybe_scheduled_guards_hour_and_switches():
    db = seed_posted(Database(Path(tempfile.mkdtemp(prefix="hirify_rep_")) / "t.db"))
    db.set_setting("post_channel_id", str(CHANNEL))
    sender = FakeSender()
    import app.reports as R
    orig = R._msk_now

    # 20:59 — ни дайджеста (порог 21), ни отчёта (порог 20, но не воскресенье? —
    # оно и есть воскресенье; проверяем именно порог часа дайджеста)
    R._msk_now = lambda: msk(hour=20)
    try:
        asyncio.run(maybe_scheduled(make_cfg(), db, sender))
        assert len([t for t in sender.texts if t[0] == CHANNEL]) == 0
        assert len([t for t in sender.texts if t[0] == ADMIN]) == 1  # отчёт ушёл
        # выключенный дайджест/отчёт (0) — вообще ничего
        sender2 = FakeSender()
        asyncio.run(maybe_scheduled(make_cfg(chan_hour=0, week_hour=0), db, sender2))
        assert sender2.texts == []
    finally:
        R._msk_now = orig
    db.close()


def test_maybe_scheduled_failure_keeps_marker():
    """Сбой отправки не двигает маркер — следующий проход попробует снова."""
    db = seed_posted(Database(Path(tempfile.mkdtemp(prefix="hirify_rep_")) / "t.db"))
    db.set_setting("post_channel_id", str(CHANNEL))
    sender = FakeSender()
    sender.fail = True
    cfg = make_cfg(week_hour=0)  # только дайджест
    import app.reports as R
    orig = R._msk_now
    R._msk_now = lambda: msk()
    try:
        asyncio.run(maybe_scheduled(cfg, db, sender))
        assert db.get_setting("chandigest_date") == ""   # маркер НЕ поставлен
        sender.fail = False
        asyncio.run(maybe_scheduled(cfg, db, sender))
        assert db.get_setting("chandigest_date") != ""    # теперь поставлен
        assert len(sender.texts) == 1
    finally:
        R._msk_now = orig
    db.close()
