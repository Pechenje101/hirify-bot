"""Регулярные отчёты конвейера канала: дайджест дня и неделя владельцу.

1) ВЕЧЕРНИЙ ДАЙДЖЕСТ КАНАЛА (по умолчанию 21:00 МСК, CHAN_DIGEST_HOUR):
   одним постом — все активные вакансии, опубликованные за сутки, ссылками
   на сами посты. Читатели получают «вечернюю сводку», канал — ритм;
2) НЕДЕЛЬНАЯ АНАЛИТИКА ВЛАДЕЛЬЦУ (воскресенье 20:00 МСК, WEEK_REPORT_HOUR=0
   — выключить): посты за неделю и состояние конвейера (очередь/закрытые/
   отклонённые). Компасс содержания. (Метрика откликов убрана вместе
   с кнопкой «Откликнуться» — решение владельца 13.11.)

Оба триггера проверяются в цикле монитора (не чаще прохода), маркеры
«когда отправляли последний раз» живут в БД — раны Actions самозамещаются,
значит повторной отправки после рестарта не будет. Опоздание в тот же день
догоняется (час >= порога, маркер не сегодняшнего дня): даунтайм не
оставляет день без сводки.
"""
from __future__ import annotations

import html as html_mod
import logging
from datetime import datetime, timedelta, timezone

from .moderation import moderation_target

E = html_mod.escape
log = logging.getLogger("hirify.reports")

# Сколько вакансий влезает в вечерний дайджест (одним постом, читается сверху)
DIGEST_ITEMS_MAX = 10

_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _msk_now() -> datetime:
    """Текущее время по МСК (UTC+3, без переходов) — tz-осведнлённое."""
    return datetime.now(timezone.utc) + timedelta(hours=3)


def _date_ru(d: datetime) -> str:
    return f"{d.day} {_MONTHS_RU[d.month - 1]}"


# ---------------------------------------------------------------- рендеры

def channel_post_link(link_base: str, msg_id: int) -> str:
    return f"{link_base.rstrip('/')}/{int(msg_id)}"


def render_channel_digest(rows: list[dict], link_base: str, now_msk: datetime) -> str:
    """Карточка вечернего дайджеста канала: заголовок-ссылка на каждый пост.

    rows — db.posted_since(): {job_id, title, company, salary, chan_msg_id}.
    Порядок старые→новые (как читается канал за день). Ссылки на посты
    работают и в публичном канале (t.me/<имя>/<id>), и в приватном
    (t.me/c/<внутренний id>/<id> — открывается у подписчиков).
    """
    head = f"📋 <b>Вакансии дня</b> — {_date_ru(now_msk)}\n"
    if not rows:
        return head + "\nСегодня новых вакансий не было — загляните завтра."
    lines = []
    for i, r in enumerate(rows[:DIGEST_ITEMS_MAX], start=1):
        title = E(str(r.get("title") or "вакансия").strip() or "вакансия")
        meta = " · ".join(x for x in (
            str(r.get("company") or "").strip(),
            str(r.get("salary") or "").strip(),
        ) if x)
        line = f"{i}. <a href=\"{channel_post_link(link_base, int(r['chan_msg_id']))}\">{title}</a>"
        if meta:
            line += f" — <i>{E(meta)}</i>"
        lines.append(line)
    tail = f"\nВсего за день: {len(rows)}" if len(rows) > len(rows[:DIGEST_ITEMS_MAX]) else ""
    return head + "\n" + "\n".join(lines) + tail


def render_week_report(db, now_msk: datetime) -> str:
    """Недельная сводка конвейера канала — в личку владельцу."""
    since = (now_msk - timedelta(days=7)).astimezone(timezone.utc).isoformat(timespec="seconds")
    posts = db.posted_count_since(since)
    st = db.moderation_stats()
    end = _date_ru(now_msk)
    start = _date_ru(now_msk - timedelta(days=6))
    out = [f"📊 <b>Неделя канала</b> — {start}…{end}\n"]
    out.append(f"Постов в канале: <b>{posts}</b>")
    out.append(
        f"\nВсего по конвейеру: опубликовано {st['posted']} · закрыто {st['closed']}"
        f" · отклонено {st['rejected']} · в очереди {st['queue']}"
    )
    return "\n".join(out)


# ---------------------------------------------------------------- расписание

async def _link_base(bot, target: int) -> str:
    """Префикс ссылок на посты канала: у публичного — t.me/<имя>,
    у приватного — t.me/c/<внутренний id> (работает для подписчиков)."""
    try:
        chat = await bot.get_chat(target)
        if getattr(chat, "username", None):
            return f"https://t.me/{chat.username}"
    except Exception as e:  # noqa: BLE001 — дайджест уйдёт с внутренними ссылками
        log.warning("get_chat(%s) не прошёл: ссылки дайджеста через t.me/c/…: %s", target, e)
    s = str(target)
    inner = s[4:] if s.startswith("-100") else s.lstrip("-")
    return f"https://t.me/c/{inner}"


async def maybe_scheduled(cfg, db, sender) -> None:
    """Точка входа из цикла монитора: дайджест дня + недельный отчёт.

    Вызывается каждый проход — дешёвая проверка «час >= порога и маркер
    не сегодняшний» в БД. Отправка — одним сообщением; сбой НЕ роняет цикл
    и не двигает маркер (следующий проход попробует снова).
    """
    msk = _msk_now()
    target = moderation_target(cfg, db)

    # --- вечерний дайджест канала
    if target and cfg.chan_digest_hour > 0 and msk.hour >= cfg.chan_digest_hour:
        today = msk.date().isoformat()
        if db.get_setting("chandigest_date", "") != today:
            since = db.get_setting("chandigest_since", "") or (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat(timespec="seconds")
            rows = db.posted_since(since, DIGEST_ITEMS_MAX)
            try:
                html = render_channel_digest(rows, await _link_base(sender.bot, target), msk)
                await sender.send_text(target, html)
                db.set_setting("chandigest_since", datetime.now(timezone.utc).isoformat(timespec="seconds"))
                db.set_setting("chandigest_date", today)
                log.info("Вечерний дайджест канала ушёл: %d позиций", len(rows))
            except Exception as e:  # noqa: BLE001 — маркер не двигаем: попробуем в следующий проход
                log.warning("Вечерний дайджест не ушёл: %s", e)

    # --- недельная аналитика владельцу (воскресенье)
    if (cfg.week_report_hour > 0 and cfg.admin_chat_id
            and msk.weekday() == 6 and msk.hour >= cfg.week_report_hour):
        iso = msk.isocalendar()
        week = f"{iso.year}-W{iso.week}"
        if db.get_setting("weekreport_week", "") != week:
            try:
                await sender.send_text(cfg.admin_chat_id, render_week_report(db, msk))
                db.set_setting("weekreport_week", week)
                log.info("Недельный отчёт конвейера ушёл владельцу")
            except Exception as e:  # noqa: BLE001 — попробуем в следующий проход того же дня
                log.warning("Недельный отчёт не ушёл: %s", e)
