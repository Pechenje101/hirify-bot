"""Команды бота (aiogram Router) + страховка от необработанных ошибок."""
from __future__ import annotations

import asyncio
import html as html_mod
import json
import logging
import random
import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import texts
from .cards import demo_vacancy
from .config import Feed
from .db import DEFAULT_DISPLAY, DISPLAY_MODES
from .hirify import HirifyAutomationError, HirifyRestrictedError
from .monitor import POLL_JITTER
from .parsing import _api_contacts
from .services import Services
from .textparse import looks_like_vacancy, parse_vacancy_text

log = logging.getLogger("hirify.handlers")

router = Router()

# Не даём /fresh долбить фид: если проход был совсем недавно — просто отвечаем
FRESH_COOLDOWN_SEC = 20

# Метки видов карточек в /settings (порядок = порядок кнопок)
DISPLAY_LABELS: dict[str, str] = {
    "bubble": "🫧 Пузырь — новый Telegram",
    "rich": "✨ Rich-сообщение",
    "classic": "📄 Классика",
    "auto": "🤖 Авто",
}
DISPLAY_ORDER = ("bubble", "rich", "classic", "auto")

# Авто-карточка из своего текста: без команды /card — просто прислать текст
# (или файл .txt/.md). Текст сообщения ≤ 4000, файл — до 512 КБ / 8000 символов:
# больше файла — точно не описание вакансии, а вырезать «середину» нельзя.
AUTOCARD_TEXT_MAX = 8000
AUTOCARD_DOC_MAX_BYTES = 512 * 1024
_DOC_EXTS = (".txt", ".md", ".markdown", ".text")


# ---------------------------------------------------------------- вспомог.

def _settings_keyboard(
    feeds: list[Feed],
    chosen: set[str] | None,
    silent: bool,
    display: str = DEFAULT_DISPLAY,
) -> InlineKeyboardMarkup:
    """Клавиатура /settings: тумблеры разделов + вид карточек + тихий режим + сброс.

    chosen=None — «по умолчанию» (все разделы из HIRIFY_FEEDS) — в UI все
    разделы показываются включёнными. display — выбранный вид карточек
    (radio: ● выбранный, ○ остальные).
    """
    rows: list[list[InlineKeyboardButton]] = []
    for f in feeds:
        on = chosen is None or f.key in chosen
        mark = "✅" if on else "⬜️"
        rows.append(
            [InlineKeyboardButton(text=f"{mark} {f.label}", callback_data=f"set:feed:{f.key}")]
        )
    # вид карточек: 2×2 radio-сетка
    for i in range(0, len(DISPLAY_ORDER), 2):
        row = []
        for mode in DISPLAY_ORDER[i:i + 2]:
            mark = "●" if mode == display else "○"
            row.append(InlineKeyboardButton(
                text=f"{mark} {DISPLAY_LABELS[mode]}",
                callback_data=f"set:display:{mode}",
            ))
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(
            text="🔕 Тихий режим: вкл" if silent else "🔔 Звуковые уведомления: вкл",
            callback_data="set:silent",
        )]
    )
    rows.append([InlineKeyboardButton(text="↩️ Как по умолчанию", callback_data="set:reset")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fmt_cookie(client) -> str:
    if not client.has_cookie:
        return "❌ не задан"
    c = client.cookie
    return f"✅ задан ({len(c)} символов, начинается с «{html_mod.escape(c[:14])}…»)"


async def _ensure_private(message: Message) -> bool:
    """Личные команды работают только в личке; в группах вежливо подсказываем."""
    if message.chat.type == "private":
        return True
    try:
        await message.answer(texts.PRIVATE_ONLY)
    except Exception:  # noqa: BLE001
        pass
    return False


def _is_admin(cfg, message: Message) -> bool:
    """Гард критичных команд: если задан ADMIN_CHAT_ID — чат должен совпадать.
    Не задан (личный бот) — пропускаем всех (прежнее поведение)."""
    if not cfg.admin_chat_id:
        return True
    return message.chat.id == cfg.admin_chat_id


async def _ensure_admin(message: Message, services: Services) -> bool:
    """Личный чат И (если задан ADMIN_CHAT_ID) — только владелец бота.
    Так /setcookie, /pause, /resume не сможет вызвать случайный пользователь,
    нашедший бота: без гарда он мог бы подменить cookie или остановить
    мониторинг для всех подписчиков."""
    if not await _ensure_private(message):
        return False
    if not _is_admin(services.cfg, message):
        try:
            await message.answer("🔒 Команда доступна только администратору бота.")
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


# ---------------------------------------------------------------- команды

@router.message(CommandStart())
async def cmd_start(message: Message, services: Services):
    if not await _ensure_private(message):
        return
    services.db.add_subscriber(message.chat.id, message.from_user.username if message.from_user else None)
    await message.answer(
        texts.WELCOME.format(rhythm=texts.poll_rhythm(services.cfg.poll_minutes)),
        parse_mode="HTML", disable_web_page_preview=True,
    )
    # сразу покажем демо-карточку (в выбранном подписчиком виде), чтобы был понятен формат
    sub = services.db.subscriber(message.chat.id)
    await services.sender.send_vacancy(
        message.chat.id, demo_vacancy(),
        display=sub["display"] if sub else DEFAULT_DISPLAY,
    )
    # и тут же проверим фид — новые вакансии уйдут без ожидания интервала
    services.monitor.nudge()


@router.message(Command("help"))
async def cmd_help(message: Message, services: Services):
    await message.answer(
        texts.HELP.format(rhythm=texts.poll_rhythm(services.cfg.poll_minutes)),
        parse_mode="HTML", disable_web_page_preview=True,
    )


@router.message(Command("test"))
async def cmd_test(message: Message, services: Services):
    """Демо-карточка в том виде, который выбран в /settings."""
    sub = services.db.subscriber(message.chat.id)
    await services.sender.send_vacancy(
        message.chat.id, demo_vacancy(),
        display=sub["display"] if sub else DEFAULT_DISPLAY,
    )


async def _card_summary(message: Message, v) -> None:
    """Короткая сводка распознанного — сразу видно качество разбора."""
    await message.answer(
        texts.CARD_SUMMARY.format(
            title=html_mod.escape(v.title or "—"),
            company=html_mod.escape(v.company) if v.company else "—",
            salary=html_mod.escape(v.salary) if v.salary else "не распознана",
            contact=(html_mod.escape(v.primary_contact.display)
                     if v.primary_contact else "не найден"),
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _send_all_formats(chat_id: int, v, services: Services) -> None:
    """Карточка во всех наших видах подряд (Пузырь → Rich → Классика).

    Это разовый рендер «на посмотреть»: вид из /settings тут не важен. Перед
    каждой карточкой — метка с номером, а если выбранный вид в этом чате не
    поддерживается — честная приписка, чем заменили (send_vacancy фолбэчит
    сам и возвращает фактический вид).
    """
    modes = ("bubble", "rich", "classic")
    for i, mode in enumerate(modes, start=1):
        label = texts.DISPLAY_LABELS_RU.get(mode, mode)
        await services.sender.send_text(chat_id, f"<b>{i}/{len(modes)}</b> · {label}")
        try:
            sent_with = await services.sender.send_vacancy(chat_id, v, display=mode, silent=True)
        except Exception as e:  # noqa: BLE001 — вид не доставился вовсе: остальное покажем
            log.warning("Авто-карточка: вид %s не доставлен: %s", mode, e)
            continue
        if sent_with != mode:
            got = texts.DISPLAY_LABELS_RU.get(sent_with, sent_with)
            await services.sender.send_text(
                chat_id, texts.AUTOCARD_FELLBACK.format(asked=label, got=got))


@router.message(Command("card"))
async def cmd_card(message: Message, command: CommandObject, services: Services):
    """Карточка из СВОЕГО текста вакансии: /card <текст> — во всех наших видах.

    Парсер распознаёт заголовок/компанию/зарплату/формат/грейд/скиллы/контакты
    (app/textparse.py), дальше карточка идёт тремя видами подряд (Пузырь,
    Rich, классика) — удобно выбрать, какой нравится (в /settings — для
    рассылки). В БД мониторинга ничего не пишется: это разовый рендер.
    То же самое бот делает сам, если просто прислать текст вакансии
    сообщением или файлом .txt/.md (см. fallback/on_document ниже).
    """
    if not await _ensure_private(message):
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(texts.CARD_HINT, parse_mode="HTML",
                             disable_web_page_preview=True)
        return
    # лимит одного сообщения Telegram 4096 — команда занимает «/card », берём
    # запас; длиннее пользовательским текстом всё равно не прислать
    raw = raw[:4000]
    v = parse_vacancy_text(raw)
    await _card_summary(message, v)
    await _send_all_formats(message.chat.id, v, services)


@router.message(Command("fresh"))
async def cmd_fresh(message: Message, command: CommandObject, services: Services):
    if not await _ensure_private(message):
        return
    services.db.add_subscriber(message.chat.id, message.from_user.username if message.from_user else None)
    limit = 10
    if command.args:
        try:
            limit = max(1, min(30, int(command.args.split()[0])))
        except (ValueError, IndexError):
            pass
    if services.monitor.busy:
        await message.answer("⏳ Уже проверяю — одну секунду…")
        return
    if time.time() - services.monitor.last_poll_ts < FRESH_COOLDOWN_SEC:
        await message.answer(texts.FRESH_COOLDOWN)
        return
    await message.answer(texts.FRESH_STARTED)
    stats = await services.monitor.poll_once(source="manual", limit=limit)
    if stats.get("automation_pause"):
        # сайт держит флаг автоматизации: честно объясняем, а не «не нашлось»
        left = int(services.monitor.automation_left_sec / 60)
        await message.answer(texts.FRESH_AUTOMATION.format(left=left))
        return
    extra = ""
    if stats.get("no_audience"):
        extra = (
            f"\nℹ️ Ещё {stats['no_audience']} обработал, но подписчиков этих разделов "
            "не нашлось — проверьте ⚙️ /settings."
        )
    if stats.get("stale"):
        extra += (
            f"\n🕰 Ещё {stats['stale']} вакансий опубликованы слишком давно — не присылал."
        )
    if stats["total"] == 0:
        await message.answer(texts.FRESH_EMPTY)
    else:
        await message.answer(
            texts.FRESH_TMPL.format(
                sent=stats["sent"], total=stats["total"],
                skipped=stats["skipped"], filtered=stats.get("filtered", 0),
            ) + extra,
            parse_mode="HTML",
        )


@router.message(Command("stats"))
async def cmd_stats(message: Message, services: Services):
    s = services.db.stats()
    await message.answer(
        texts.STATS_TMPL.format(**{k: s.get(k, 0) for k in
                                   ("total", "sent", "sent_today", "skipped", "filtered",
                                    "stale", "failed", "fetch_failed", "send_failed")}),
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message, services: Services):
    # cookie и состояние мониторинга — не для общего чата: показываем только в личке
    if not await _ensure_private(message):
        return
    paused = "⏸ выключен (/resume — включить)" if services.monitor.paused else "▶️ работает"
    automation_left = services.monitor.automation_left_sec
    automation = (
        f"🛡 ещё {automation_left / 60:.0f} мин (сайт закрыл просмотры)"
        if automation_left > 0 else "нет"
    )
    fmt = (
        "Bubble/Rich/Классика — у каждого свой выбор в /settings"
        if services.cfg.use_widget and services.cfg.use_rich
        else ("Rich + классика" if services.cfg.use_rich else "классический HTML")
    )
    feeds = " · ".join(f.label for f in services.cfg.feeds)
    # честный интервал: базовые N мин × джиттер — показываем диапазон, а не «ровно N»
    base = services.cfg.poll_minutes
    interval = f"~{base * POLL_JITTER[0]:.1f}–{base * POLL_JITTER[1]:.1f}"
    await message.answer(
        texts.STATUS_TMPL.format(
            paused=paused,
            interval=interval,
            fast_sec=int(services.cfg.fast_poll_sec),
            feeds=html_mod.escape(feeds),
            cookie=_fmt_cookie(services.client),
            rich=fmt,
            automation=automation,
            subs=len(services.db.subscriber_ids()),
            params=html_mod.escape(services.cfg.feed_params),
        ),
        parse_mode="HTML",
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, services: Services):
    """Персональные настройки рассылки: разделы, вид карточек, тихий режим (только личка)."""
    if not await _ensure_private(message):
        return
    services.db.add_subscriber(
        message.chat.id, message.from_user.username if message.from_user else None
    )
    sub = services.db.subscriber(message.chat.id)
    chosen, silent = sub["feeds"], sub["silent"]
    display = sub["display"]
    eff = [
        f.label for f in services.cfg.feeds
        if chosen is None or f.key in chosen
    ] or ["— ничего не выбрано (вакансии приходить не будут)"]
    await message.answer(
        texts.SETTINGS_TMPL.format(
            feeds=html_mod.escape(" · ".join(eff)),
            mode="🔕 тихий (без звука)" if silent else "🔔 обычные",
            display=texts.DISPLAY_LABELS_RU.get(display, display),
        ),
        reply_markup=_settings_keyboard(services.cfg.feeds, chosen, silent, display),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("set:"))
async def on_settings_callback(cb: CallbackQuery, services: Services):
    """Тумблеры /settings: применяем сразу, обновляем клавиатуру на месте."""
    if cb.message is None or cb.message.chat.type != "private" or cb.from_user is None:
        await cb.answer("\U0001F512 Настройки работают только в личном чате", show_alert=True)
        return
    chat_id = cb.message.chat.id
    sub = services.db.subscriber(chat_id)
    chosen: set[str] | None = sub["feeds"] if sub else None
    silent: bool = sub["silent"] if sub else False
    display: str = sub["display"] if sub else DEFAULT_DISPLAY
    all_keys = {f.key for f in services.cfg.feeds}

    parts = (cb.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "feed" and len(parts) == 3 and parts[2] in all_keys:
        cur = set(chosen) if chosen is not None else set(all_keys)
        cur ^= {parts[2]}  # тумблер: был включён — выключаем, и наоборот
        chosen = cur
    elif action == "display" and len(parts) == 3 and parts[2] in DISPLAY_MODES:
        display = parts[2]
    elif action == "silent":
        silent = not silent
    elif action == "reset":
        chosen = None
    else:
        await cb.answer()
        return

    services.db.update_subscriber_settings(
        chat_id, feeds=sorted(chosen) if chosen is not None else None,
        silent=silent, display=display,
    )
    try:
        await cb.message.edit_reply_markup(
            reply_markup=_settings_keyboard(services.cfg.feeds, chosen, silent, display)
        )
    except Exception:  # noqa: BLE001 — старое сообщение могли удалить
        pass
    if action == "display":
        # сразу показываем карточку в новом виде: видно, чем именно теперь
        # приходят вакансии (пузырь/rich/классика), без ожидания новой вакансии
        label = texts.DISPLAY_LABELS_RU.get(display, display)
        sent = False
        try:
            await services.sender.send_vacancy(
                chat_id, demo_vacancy(), display=display, silent=True,
            )
            sent = True
        except Exception:  # noqa: BLE001 — демо не критично, настройка уже сохранена
            pass
        note = " — отправил демо-карточку" if sent else ""
        await cb.answer(f"Вид карточек: {label}{note}")
    else:
        await cb.answer("✅ Сохранено")


# ------------------------------------------------ кнопка «Показать контакты»
# Кнопочный режим (CONTACTS_MODE=button): карточка уходит из анонимных данных,
# а контакт человек получает одним нажатием — ровно один auth-запрос от
# живого интереса. Это тот паттерн, который сайт ожидает от человека;
# пачек авто-просмотров (главный триггер automation_detected) больше нет.

_CNT_LAST: dict[tuple[int, int], float] = {}
_CNT_COOLDOWN_SEC = 20  # анти-даблклик; кнопка остаётся на карточке


async def fetch_contacts_for_job(job_id: int, services: Services) -> tuple[str, list[dict]]:
    """Ядро кнопки: возвращает (исход, контакты-словари).

    Исходы: "cached" / "ok" — контакты есть (кэш БД или свежий запрос);
    "empty" — сайт контактов не отдал; "noid" — вакансии нет в базе;
    "pause:N" — флаг держит паузу ещё N мин; "quota" — лимит исчерпан;
    "noauth" — cookie не задан; "restricted" — контакты недоступны;
    "flag:N" — запрос словил флаг, пауза N мин поставлена; "fail" — сбой.
    """
    db, mon, client = services.db, services.monitor, services.client
    job = db.get_job(job_id)
    if job is None:
        return "noid", []
    try:
        cached = json.loads(job.get("tg_contacts") or "[]")
    except (TypeError, ValueError):
        cached = []
    if cached:
        return "cached", cached

    left = mon.automation_left_sec
    if left > 0:
        return f"pause:{left / 60:.0f}", []
    if mon.auth_quota_block():
        return "quota", []
    if not client.has_cookie:
        return "noauth", []

    # полный слаг хранится в URL вакансии (fetch_contacts ждёт <id>-<slug>)
    full_slug = (job.get("url") or "").rstrip("/").rsplit("/", 1)[-1] or job.get("slug") or ""
    if not full_slug:
        return "noid", []
    try:
        await asyncio.sleep(random.uniform(0.6, 2.5))  # живой темп, не метроном
        contacts_data = await client.fetch_contacts(full_slug)
    except HirifyAutomationError as e:
        minutes = mon.register_automation_hit(e, [])
        return f"flag:{minutes}", []
    except HirifyRestrictedError:
        return "restricted", []
    except Exception as e:  # noqa: BLE001 — сеть/таймаут/5xx
        log.warning("Кнопка «контакты» (%s): %s", full_slug, e)
        return "fail", []
    mon.auth_view_spent()

    contacts = _api_contacts(contacts_data or {})
    if not contacts:
        return "empty", []
    entries = [{"kind": c.kind, "value": c.value, "display": c.display} for c in contacts]
    db.update_job_parsed(job_id, tg_contacts=entries)
    mon.automation_recovered()
    return "ok", entries


async def _reply_contacts(services: Services, chat_id: int, job: dict, entries: list[dict]) -> None:
    """Сообщение с контактами (после кнопки): коротко + кнопка «Написать»."""
    title = html_mod.escape(str(job.get("title") or "вакансия"))
    rows = [f"• <b>{html_mod.escape(str(e.get('display') or e.get('value') or ''))}</b>"
            for e in entries[:3] if e.get("display") or e.get("value")]
    text = f"👤 Контакты — {title}:\n" + "\n".join(rows)
    buttons = []
    for e in entries[:2]:
        value, kind = str(e.get("value") or ""), str(e.get("kind") or "")
        if kind == "username" and value:
            buttons.append(InlineKeyboardButton(text=f"✉️ @{value}", url=f"https://t.me/{value}"))
    markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
    await services.sender.send_text(chat_id, text, markup=markup)


@router.callback_query(F.data.startswith("cnt:"))
async def on_contacts_button(cb: CallbackQuery, services: Services):
    """Кнопка «Показать контакты»: один auth-запрос от человека."""
    try:
        job_id = int(str(cb.data or "").split(":", 1)[1])
    except ValueError:
        await cb.answer("Неизвестная вакансия", show_alert=True)
        return
    uid = cb.from_user.id if cb.from_user else 0
    now = time.monotonic()
    if now - _CNT_LAST.get((uid, job_id), 0.0) < _CNT_COOLDOWN_SEC:
        await cb.answer("Уже обрабатываю — подождите пару секунд")
        return
    _CNT_LAST[(uid, job_id)] = now

    outcome, entries = await fetch_contacts_for_job(job_id, services)
    if entries and outcome in ("cached", "ok"):
        await cb.answer()
        job = services.db.get_job(job_id) or {"title": ""}
        chat_id = cb.message.chat.id if cb.message else uid
        await _reply_contacts(services, chat_id, job, entries)
        return
    if outcome == "noid":
        await cb.answer("Вакансия уже не найдена в базе", show_alert=True)
    elif outcome.startswith("pause:"):
        await cb.answer(f"hirify временно ограничил просмотры (≈{outcome.split(':', 1)[1]} мин) — попробуйте позже", show_alert=True)
    elif outcome == "quota":
        await cb.answer("Лимит просмотров на час исчерпан — попробуйте позже", show_alert=True)
    elif outcome == "noauth":
        await cb.answer("Cookie не задан — контакты недоступны (см. /help)", show_alert=True)
    elif outcome == "restricted":
        await cb.answer("Контакты этой вакансии недоступны (премиум/скрыто)", show_alert=True)
    elif outcome.startswith("flag:"):
        await cb.answer(f"hirify ограничил просмотры на {outcome.split(':', 1)[1]} мин — попробуйте позже", show_alert=True)
    elif outcome in ("empty", "fail"):
        await cb.answer("Не получилось достать контакты — попробуйте позже", show_alert=True)
    else:
        await cb.answer()


@router.message(Command("pause"))
async def cmd_pause(message: Message, services: Services):
    if not await _ensure_admin(message, services):
        return
    services.monitor.pause()
    await message.answer(texts.PAUSED)


@router.message(Command("resume"))
async def cmd_resume(message: Message, services: Services):
    if not await _ensure_admin(message, services):
        return
    services.monitor.resume()
    await message.answer(texts.RESUMED)


@router.message(Command("unsubscribe"))
async def cmd_unsub(message: Message, services: Services):
    services.db.remove_subscriber(message.chat.id)
    await message.answer(texts.UNSUBSCRIBED)


@router.message(Command("setcookie"))
async def cmd_setcookie(message: Message, command: CommandObject, services: Services):
    """Обновление cookie прямо в чате. Строка не сохраняется в логах.
    Только администратор (см. _ensure_admin): чужой cookie подменит сессию
    и рассылка пойдёт из-под чужого аккаунта."""
    if not await _ensure_admin(message, services):
        return
    raw = (command.args or "").strip()
    if not raw or "=" not in raw or len(raw) < 20:
        await message.answer(
            "❌ Похоже, это не cookie. Пришлите так:\n"
            "<code>/setcookie имя1=значение1; имя2=значение2</code>\n"
            "Как скопировать — /help, шаг 2.",
            parse_mode="HTML",
        )
        return
    # Telegram съедает переносы; принимаем и многострочные варианты
    raw = raw.replace("\n", " ")
    services.client.set_cookie(raw)
    services.db.set_setting("hirify_cookie", raw)
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass
    # В режиме GitHub Actions БД коммитится в (публичный) репозиторий: cookie из неё
    # вычищается перед коммитом, поэтому живёт он только в секретах репозитория
    if services.cfg.run_max_minutes > 0:
        await message.answer(texts.COOKIE_SET + "\n\n" + texts.COOKIE_SET_EPHEMERAL)
    else:
        await message.answer(texts.COOKIE_SET)


def _decode_doc(b: bytes) -> str:
    """Текст файла: utf-8 (с BOM), иначе cp1251, иначе замены символов."""
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


@router.message(F.document)
async def on_document(message: Message, services: Services):
    """Загруженное описание вакансии: файл .txt/.md → карточка во всех видах.

    «Загрузить описание» — буквально: не надо ничего копировать в сообщение.
    Только личка (в группах молчим), только текстовые файлы (картинки/PDF
    бот не читает), до 512 КБ. Дальше — тот же конвейер, что у вставленного
    текста: детект «похоже на вакансию» → сводка → три вида карточки.
    """
    if message.chat.type != "private":
        return
    doc = message.document
    name = (doc.file_name or "").lower() if doc else ""
    mime = (doc.mime_type or "") if doc else ""
    is_text_doc = bool(doc) and (
        name.endswith(_DOC_EXTS) or mime.startswith("text/") or not name
    )
    if not is_text_doc or (doc.file_size or 0) > AUTOCARD_DOC_MAX_BYTES:
        await message.answer(texts.DOC_HINT, parse_mode="HTML",
                             disable_web_page_preview=True)
        return
    data = await message.bot.download(doc)
    if data is None:
        await message.answer(texts.DOC_HINT, parse_mode="HTML",
                             disable_web_page_preview=True)
        return
    raw_bytes = data.getvalue() if hasattr(data, "getvalue") else bytes(data)
    raw = _decode_doc(raw_bytes)[:AUTOCARD_TEXT_MAX]
    if not looks_like_vacancy(raw):
        # файл прочитан, но вакансии в нём не видно — объясняем, что распознаём
        await message.answer(texts.CARD_HINT, parse_mode="HTML",
                             disable_web_page_preview=True)
        return
    v = parse_vacancy_text(raw)
    await _card_summary(message, v)
    await _send_all_formats(message.chat.id, v, services)


@router.message()
async def fallback(message: Message, services: Services):
    """Все прочие сообщения: в личке — авто-карточка, если текст похож на
    вакансию (вставил описание без команды — бот соберёт карточку сам),
    иначе подсказка. В группах МОЛЧИМ: бот, добавленный в группу, не должен
    отвечать на каждое сообщение чата (спам)."""
    if message.chat.type != "private":
        return
    if message.text and looks_like_vacancy(message.text):
        v = parse_vacancy_text(message.text[:4000])
        await _card_summary(message, v)
        await _send_all_formats(message.chat.id, v, services)
        return
    await message.answer(texts.FALLBACK_HINT, parse_mode="HTML")


# ---------------------------------------------------------------- ошибки

@router.errors()
async def on_error(event: ErrorEvent):
    """Необработанная ошибка в хендлере: лог + честный ответ пользователю."""
    log.exception("Необработанная ошибка апдейта: %s", event.exception)
    message = event.update.message
    if message is not None:
        try:
            await message.answer(texts.ERROR_TEXT)
        except Exception:  # noqa: BLE001
            pass
    return True
