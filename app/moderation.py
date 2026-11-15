"""Модерация публикаций: карточки вакансий → решение владельца → пост в канал.

Конвейер (канал владельца — «конвейер контента», новая роль бота с 13.11):
1) каждая доставленная админу вакансия ОДНОВРЕМЕННО ставится в очередь
   модерации: в БД пишется полный payload карточки (модерация переживает
   самозамещение ранов Actions), а админу приходит компактная панель с
   кнопками «✅ Опубликовать» / «❌ Отклонить»;
2) «Опубликовать» — карточка отправляется в канал нативным постом тем же
   конвейером (sender.send_vacancy), ВСЕГДА КЛАССИКОЙ (текст): такой пост
   потом правит «🔒 Закрыть» через editMessageText, который rich/виджеты
   редактировать не умеет. Карточка ЧИСТАЯ — как в рассылке:
   без футера-источника и хештегов, единственная кнопка — «Написать
   рекрутеру» (воронки «Откликнуться» в личку бота больше нет — решение
   владельца 13.11). Запоминаются id поста и время (posted_at, chan_msg_id);
3) после публикации панель решения сменяется панелью управления:
   «🔒 Закрыть» (правит пост: замочек, кнопки сняты) и
   «🗑 Удалить пост»;
4) очередь и решения живут в БД (jobs.mod_status): /pending показывает
   нерешённые панели заново после любого рестарта;
5) РУЧНЫЕ вакансии (владелец прислал описание сам — текстом, /card или
   файлом .txt/.md) с 15.11 идут тем же конвейером: offer_manual_
   moderation ставит карточку в очередь — «✅ Опубликовать» постит её
   в канал, как вакансии из фида.

Канал публикации задаётся /setchannel <@имя | -100…> (админ; хранится в БД)
или POST_CHANNEL_ID в окружении. Если бот уже добавлен в канал админом,
канал привяжется и САМ — по первому посту в нём (auto_bind_channel:
апдейты channel_post приходят боту-админу). Модерация включается, когда
заданы И канал, И ADMIN_CHAT_ID: панель приходит в личный чат владельца.
"""
from __future__ import annotations

import html as html_mod
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .cards import render_classic_html
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


async def auto_bind_channel(chat_id: int, title: str, cfg, db, sender) -> bool:
    """Авто-привязка канала публикаций — бот уже добавлен админом.

    Апдейты channel_post приходят боту в КАЖДЫЙ канал, где он админ: первый
    пост в ещё не привязанном канале запоминает его (настройка в БД) и
    уведомляет владельца. Явно заданный канал (/setchannel или
    POST_CHANNEL_ID) подменять нельзя — защита от случайного канала.
    Возвращает True, если привязка произошла.
    """
    if moderation_target(cfg, db):
        return False
    db.set_setting("post_channel_id", str(chat_id))
    log.info("Канал публикаций привязан автоматически: %s (%s)", title, chat_id)
    if cfg.admin_chat_id:
        shown = title.strip() or str(chat_id)
        try:
            await sender.send_text(
                cfg.admin_chat_id,
                f"📡 Канал публикаций привязан автоматически: <b>{E(shown)}</b>\n\n"
                "Каждая новая карточка теперь приходит с кнопками"
                " «✅ Опубликовать» / «❌ Отклонить», а «✅ Опубликовать» —"
                " постит её в этот канал нативным постом.\n"
                "Очередь решений: /pending · не тот канал? /setchannel &lt;@имя&gt;"
                " · выключить: /setchannel off",
            )
        except Exception as e:  # noqa: BLE001 — уведомление не критично
            log.warning("Уведомление о привязке канала %s не ушло: %s", chat_id, e)
    return True


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


def manage_keyboard(job_id: int) -> InlineKeyboardMarkup:
    """Панель УПРАВЛЕНИЯ опубликованным постом (вместо панели решения):
    закрыть вакансию (дописать «🔒 закрыта», снять кнопки) или удалить
    пост из канала совсем."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"mod:close:{job_id}"),
        InlineKeyboardButton(text="🗑 Удалить пост", callback_data=f"mod:del:{job_id}"),
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


async def offer_manual_moderation(user_id: int, v, services) -> str:
    """Ручная вакансия (текст/файл от владельца) → очередь модерации.

    Вызывается из авто-карточек после рендера: владелец прислал описание
    вакансии сам — бот собрал карточку, и под ней появляется та же панель
    «✅ Опубликовать / ❌ Отклонить», что у вакансий из фида. job_id
    текстовых вакансий детерминированный (crc32 текста, 700M+): тот же
    текст снова → та же вакансия, панель не дублируется.

    Исходы:
    'off'     — канал публикаций не задан (модерация выключена);
    'foreign' — приславший не владелец (канал владельца — его решения);
    'pending' — уже в очереди (панель не дублируем, /pending покажет);
    'posted'  — уже опубликована/закрыта/удалена;
    'queued'  — панель поставлена (в т.ч. повторно после «Отклонить»).
    """
    cfg, db = services.cfg, services.db
    if not moderation_active(cfg, db):
        return "off"
    if cfg.admin_chat_id and user_id != cfg.admin_chat_id:
        return "foreign"
    job = db.get_job(v.job_id)
    status = (job.get("mod_status") or "") if job else ""
    if status == "queue":
        return "pending"
    if status in ("posted", "closed", "deleted"):
        return "posted"
    if job is None:
        # карточка уже доставлена рендером авто-карточки — это 'sent',
        # а не 'new': конвейер доставки её не ждёт
        db.add_job(
            v.job_id, v.slug, v.url or "",
            title=v.title or "", company=v.company or "", salary=v.salary or "",
            tg_contacts=[
                {"kind": c.kind, "value": c.value, "display": c.display}
                for c in v.contacts
            ],
        )
        db.mark_job(v.job_id, "sent")
    if status == "no":
        # «Отклонить» не навсегда: тот же текст снова предложит панель
        db.set_mod(v.job_id, "")
    await queue_for_moderation(v.job_id, v, cfg, db, services.sender, True)
    return "queued"


async def publish_job(job_id: int, services) -> str:
    """Опубликовать вакансию из очереди в канал. Исходы:

    'ok' — пост в канале ушёл, снят с очереди (mod_status='posted',
    записаны posted_at и id поста — кнопки «Закрыть/Удалить», дайджест дня);
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
    # Пост в канал — всегда КЛАССИКА (текст): «🔒 Закрыть» правит его потом
    # editMessageText, а тот умеет только текстовые посты — rich-сообщение
    # или виджет отредактировать нельзя (виджеты в каналы и вовсе не уходят).
    # Читателям канала классика гарантированно видна в любом клиенте.
    # Вид из /settings владельца остаётся его личным выбором рассылки.
    try:
        _mode, msg_id = await services.sender.send_vacancy(
            target, v, display="classic", want_message=True,
        )
    except Exception as e:  # noqa: BLE001 — бот не админ / флад / сеть
        log.warning("Публикация job %s в канал %s не прошла: %s", job_id, target, e)
        return "fail"
    db.mark_posted(job_id, msg_id)
    return "ok"


CLOSE_SUFFIX = "\n\n🔒 <b>Вакансия закрыта</b> — отклики больше не принимаются."


async def close_job(job_id: int, services) -> str:
    """Закрыть вакансию: редактирует пост в канале (дописывает замочек,
    снимает кнопки — рекрутеру больше писать не нужно). Исходы как у
    publish_job плюс:

    'already' — уже закрыта/решена;
    'nomsg' — id поста не записан (пост вышел без записи id) — править нечего;
    'fail' — Telegram не принял правку (пост удалили вручную?).
    """
    db, cfg = services.db, services.cfg
    target = moderation_target(cfg, db)
    if not target:
        return "nochan"
    job = db.get_job(job_id)
    if job is None:
        return "noid"
    if (job.get("mod_status") or "") != "posted":
        return "already"
    if not int(job.get("chan_msg_id") or 0):
        return "nomsg"
    v = vacancy_from_payload(job.get("payload") or "")
    if v is None:
        return "nopayload"
    try:
        await services.sender.bot.edit_message_text(
            chat_id=target,
            message_id=int(job["chan_msg_id"]),
            text=render_classic_html(v) + CLOSE_SUFFIX,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=None,  # кнопки сняты — вакансия закрыта
        )
    except Exception as e:  # noqa: BLE001 — пост могли удалить руками
        log.warning("Закрытие вакансии job %s не прошло: %s", job_id, e)
        return "fail"
    db.mark_channel_post_gone(job_id, "closed")
    return "ok"


async def delete_post(job_id: int, services) -> str:
    """Удалить пост из канала (передумали/пост устарел). Исходы те же плюс
    'already' — уже удалён/решён."""
    db, cfg = services.db, services.cfg
    target = moderation_target(cfg, db)
    if not target:
        return "nochan"
    job = db.get_job(job_id)
    if job is None:
        return "noid"
    if (job.get("mod_status") or "") != "posted":
        return "already"
    if not int(job.get("chan_msg_id") or 0):
        return "nomsg"
    try:
        await services.sender.bot.delete_message(target, int(job["chan_msg_id"]))
    except Exception as e:  # noqa: BLE001 — пост могли удалить руками
        log.warning("Удаление поста job %s не прошло: %s", job_id, e)
        return "fail"
    db.mark_channel_post_gone(job_id, "deleted")
    return "ok"
