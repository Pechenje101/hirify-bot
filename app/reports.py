"""Регулярные отчёты конвейера канала: дайджест дня и неделя владельцу.

1) ВЕЧЕРНИЙ ДАЙДЖЕСТ (по умолчанию 21:00 МСК, CHAN_DIGEST_HOUR): с 15.11 —
   ТОЛЬКО ПРОЕКТ в личку владельца с кнопками «✅ Опубликовать в канал» /
   «❌ Не надо»: бот не постит в канал НИЧЕГО без апрува владельца (его
   правило), сводка не исключение. Карточка проекта = будущий пост (тот же
   HTML хранится в БД — кнопка переживает рестарты ранов), публикация —
   только по нажатию (resolve_digest). Пустых сводок («за день не было
   вакансий») больше нет вовсе.
2) НЕДЕЛЬНАЯ АНАЛИТИКА ВЛАДЕЛЬЦУ (воскресенье 20:00 МСК, WEEK_REPORT_HOUR=0
   — выключить): посты за неделю и состояние конвейера (очередь/закрытые/
   отклонённые). Компасс содержания. Приходит ТОЛЬКО владельцу в личку.
   (Метрика откликов убрана вместе с кнопкой «Откликнуться» — решение
   владельца 13.11.)

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

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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


def render_channel_digest(rows: list[dict], link_base: str, now_msk: datetime,
                           total: int | None = None) -> str:
    """Карточка вечернего дайджеста канала: заголовок-ссылка на каждый пост.

    rows — db.posted_since(): {job_id, title, company, salary, chan_msg_id}
    (уже с LIMIT — показываем не больше DIGEST_ITEMS_MAX). total — сколько
    постов вышло за окно ВСЕГО (db.posted_count_since, включая закрытые);
    None — считаем по самим строкам. Порядок старые→новые (как читается
    канал за день). Ссылки на посты работают и в публичном канале
    (t.me/<имя>/<id>), и в приватном (t.me/c/<внутренний id>/<id> —
    открывается у подписчиков).
    """
    head = f"📋 <b>Вакансии дня</b> — {_date_ru(now_msk)}\n"
    if not rows:
        return head + "\nСегодня новых вакансий не было — загляните завтра."
    items = rows[:DIGEST_ITEMS_MAX]
    lines = []
    for i, r in enumerate(items, start=1):
        title = E(str(r.get("title") or "вакансия").strip() or "вакансия")
        meta = " · ".join(x for x in (
            str(r.get("company") or "").strip(),
            str(r.get("salary") or "").strip(),
        ) if x)
        line = f"{i}. <a href=\"{channel_post_link(link_base, int(r['chan_msg_id']))}\">{title}</a>"
        if meta:
            line += f" — <i>{E(meta)}</i>"
        lines.append(line)
    if total is None:
        total = len(rows)
    tail = f"\nВсего за день: {total}" if total > len(items) else ""
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


def digest_keyboard(day: str) -> InlineKeyboardMarkup:
    """Панель проекта дайджеста: публикация в канал — только по этой кнопке
    (решение владельца 15.11: бот не постит в канал без апрува)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать в канал", callback_data=f"dgr:ok:{day}"),
        InlineKeyboardButton(text="❌ Не надо", callback_data=f"dgr:no:{day}"),
    ]])


def _close_digest_window(db) -> None:
    """Решение по дайджесту принято: окно закрываем «сейчас» (посты,
    вошедшие в решённую сводку, завтра не повторятся), проект из БД убираем."""
    db.set_setting("chandigest_since", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    db.set_setting("chandigest_draft", "")
    db.set_setting("chandigest_draft_date", "")


async def resolve_digest(action: str, day: str, services) -> str:
    """Кнопки проекта дайджеста («Опубликовать» / «Не надо») — ядро без aiogram.

    Исходы:
    'ok'       — пост в канале вышел (окно закрыто, проект снят);
    'rejected' — отклонено: в канал не уходит, окно закрыто (завтра эти
                 посты в сводку не вернутся);
    'nodraft'  — проекта нет (уже решён/утрачен) — панель ни к чему;
    'stale'    — панель от другой даты: уже предложена более свежая сводка;
    'nochan'   — канал не задан (/setchannel);
    'fail'     — Telegram не принял пост (бот не админ? сеть?) — панель
                 остаётся, можно нажать снова.
    """
    db, cfg = services.db, services.cfg
    draft = db.get_setting("chandigest_draft", "")
    draft_day = db.get_setting("chandigest_draft_date", "")
    if not draft or not draft_day:
        return "nodraft"
    if day != draft_day:
        return "stale"
    if action == "no":
        _close_digest_window(db)
        return "rejected"
    target = moderation_target(cfg, db)
    if not target:
        return "nochan"
    try:
        await services.sender.send_text(target, draft)
    except Exception as e:  # noqa: BLE001 — пост не ушёл: панель остаётся для повтора
        log.warning("Публикация дайджеста в канал %s не прошла: %s", target, e)
        return "fail"
    _close_digest_window(db)
    return "ok"


async def maybe_scheduled(cfg, db, sender) -> None:
    """Точка входа из цикла монитора: дайджест дня + недельный отчёт.

    Вызывается каждый проход — дешёвая проверка «час >= порога и маркер
    не сегодняшний» в БД. Отправка — одним сообщением; сбой НЕ роняет цикл
    и не двигает маркер (следующий проход попробует снова).
    """
    msk = _msk_now()
    target = moderation_target(cfg, db)

    # --- вечерний дайджест: ПРОЕКТ владельцу, в канал — только по кнопке
    if target and cfg.chan_digest_hour > 0 and msk.hour >= cfg.chan_digest_hour:
        today = msk.date().isoformat()
        if db.get_setting("chandigest_date", "") != today:
            since = db.get_setting("chandigest_since", "") or (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat(timespec="seconds")
            rows = db.posted_since(since, DIGEST_ITEMS_MAX)
            # полный счётчик — «Всего за день: N» даже когда показываем 10
            total = db.posted_count_since(since)
            if not rows and not total:
                # постить нечего: пустой «сводки дня» в канал не будет вовсе.
                # Маркер дня НЕ ставим — появится пост, предложим тем же вечером
                log.debug("Дайджест: постов с %s нет — проект не формирую", since)
            elif not cfg.admin_chat_id:
                # некому апрувить: в канал молча не постим (правило владельца)
                log.debug("Дайджест: ADMIN_CHAT_ID не задан — без проекта")
            else:
                try:
                    html = render_channel_digest(
                        rows, await _link_base(sender.bot, target), msk, total=total)
                    # проект в БД переживает рестарты ранов: кнопка достанет
                    # его и после замещения, ровно как отправлено в превью
                    db.set_setting("chandigest_draft", html)
                    db.set_setting("chandigest_draft_date", today)
                    proposal = (
                        "🎛 <b>Проект вечернего дайджеста</b> — таким его увидит канал.\n"
                        "Публикация — только по вашей кнопке.\n\n" + html
                    )
                    await sender.send_text(
                        cfg.admin_chat_id, proposal, markup=digest_keyboard(today),
                    )
                    db.set_setting("chandigest_date", today)
                    log.info("Проект вечернего дайджеста ушёл владельцу: %d позиций "
                             "(публикация — по кнопке)", len(rows))
                except Exception as e:  # noqa: BLE001 — маркер не двигаем: попробуем в следующий проход
                    log.warning("Проект дайджеста не ушёл: %s", e)

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
