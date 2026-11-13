"""Модерация публикаций: карточки вакансий → решение владельца → пост в канал.

Конвейер (канал владельца — «конвейер контента», новая роль бота с 13.11):
1) каждая доставленная админу вакансия ОДНОВРЕМЕННО ставится в очередь
   модерации: в БД пишется полный payload карточки (модерация переживает
   самозамещение ранов Actions), а админу приходит компактная панель с
   кнопками «✅ Опубликовать» / «❌ Отклонить»;
2) «Опубликовать» — карточка отправляется в канал нативным постом тем же
   конвейером (sender.send_vacancy, вид как в /settings владельца, с
   автооткатом bubble→rich→классика). Карточка ЧИСТАЯ — без футера-источника
   и хештегов (решение владельца 13.11);
3) очередь и решения живут в БД (jobs.mod_status): /pending показывает
   нерешённые панели заново после любого рестарта.

Канал публикации задаётся /setchannel <@имя | -100…> (админ; хранится в БД)
или POST_CHANNEL_ID в окружении. Модерация включается, когда заданы И канал,
И ADMIN_CHAT_ID: панель приходит в личный чат владельца.
"""
from __future__ import annotations

import html as html_mod
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import DEFAULT_DISPLAY
from .parsing import vacancy_from_payload, vacancy_to_payload

# Services НЕ импортируем на уровне модуля: services.py тянет monitor.py,
# а monitor.py импортирует нас (цикл). Аннотация строкой под from __future__.

E = html_mod.escape
log = logging.getLogger("hirify.moderation")


def moderation_target(cfg, db) -> int | None:
    """Куда публиковать: настройка из БД (команда /setchannel) перекрывает
    POST_CHANNEL_ID из окружения. None — канал не задан, модерация выключена."""
    raw = (db.get_setting("post_channel_id", "") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return cfg.post_channel_id or None


def moderation_active(cfg, db) -> bool:
    """Панели шлём только когда есть и канал, и чат владельца."""
    return bool(moderation_target(cfg, db) and cfg.admin_chat_id)


# ---------------------------------------------------------------- панель

def panel_text(title: str, company: str = "", salary: str = "") -> str:
    meta = " · ".join(x for x in (company.strip(), salary.strip()) if x)
    head = f"🎛 <b>Модерация</b>\n<b>{E(title or 'вакансия')}</b>"
    return f"{head}\n<i>{E(meta)}</i>" if meta else head


def panel_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"mod:ok:{job_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod:no:{job_id}"),
    ]])


# ---------------------------------------------------------------- ядро

async def queue_for_moderation(
    job_id: int, v, cfg, db, sender, admin_recipient: bool,
) -> None:
    """Поставить вакансию в очередь модерации и послать панель админу.

    Вызывается из _deliver_vacancy ПОСЛЕ успешной доставки карточки: панель
    идёт под карточкой в том же чате. Повторную доставку (ретрай) не
    дублируем — вакансия уже в очереди или уже решена (mod_status != '').
    admin_recipient=False — карточка до владельца не дошла (его фильтры
    разделов): модерации не будет, вакансия живёт обычной рассылкой.
    """
    if not admin_recipient or not cfg.admin_chat_id:
        return
    job = db.get_job(job_id)
    if not job or (job.get("mod_status") or ""):
        return
    db.save_moderation(job_id, vacancy_to_payload(v))
    try:
        await sender.send_text(
            cfg.admin_chat_id, panel_text(v.title, v.company, v.salary),
            markup=panel_keyboard(job_id),
        )
    except Exception as e:  # noqa: BLE001 — панель не критична: /pending достанет
        log.warning("Панель модерации job %s не ушла: %s", job_id, e)


async def publish_job(job_id: int, services) -> str:
    """Опубликовать вакансию из очереди в канал. Исходы:

    'ok' — пост в канале ушёл, снят с очереди (mod_status='posted');
    'already' — уже опубликована/решена (повторное нажатие);
    'nochan' — канал не задан (/setchannel);
    'noid' — вакансии нет в базе;
    'nopayload' — payload не сохранился (карточку не восстановить);
    'fail' — Telegram не принял пост (бот не админ канала? сеть?).
    """
    db, cfg = services.db, services.cfg
    target = moderation_target(cfg, db)
    if not target:
        return "nochan"
    job = db.get_job(job_id)
    if job is None:
        return "noid"
    if (job.get("mod_status") or "") != "queue":
        return "already"
    v = vacancy_from_payload(job.get("payload") or "")
    if v is None:
        return "nopayload"
    sub = db.subscriber(cfg.admin_chat_id) if cfg.admin_chat_id else None
    display = sub["display"] if sub else DEFAULT_DISPLAY
    try:
        await services.sender.send_vacancy(target, v, display=display)
    except Exception as e:  # noqa: BLE001 — бот не админ / флад / сеть
        log.warning("Публикация job %s в канал %s не прошла: %s", job_id, target, e)
        return "fail"
    db.set_mod(job_id, "posted")
    return "ok"
