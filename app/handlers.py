"""Команды бота (aiogram Router) + страховка от необработанных ошибок."""
from __future__ import annotations

import html as html_mod
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import texts
from .cards import demo_vacancy
from .config import Feed
from .db import DEFAULT_DISPLAY, DISPLAY_MODES
from .services import Services

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


# ---------------------------------------------------------------- команды

@router.message(CommandStart())
async def cmd_start(message: Message, services: Services):
    if not await _ensure_private(message):
        return
    services.db.add_subscriber(message.chat.id, message.from_user.username if message.from_user else None)
    await message.answer(texts.WELCOME, parse_mode="HTML", disable_web_page_preview=True)
    # сразу покажем демо-карточку (в выбранном подписчиком виде), чтобы был понятен формат
    sub = services.db.subscriber(message.chat.id)
    await services.sender.send_vacancy(
        message.chat.id, demo_vacancy(),
        display=sub["display"] if sub else DEFAULT_DISPLAY,
    )
    # и тут же проверим фид — новые вакансии уйдут без ожидания интервала
    services.monitor.nudge()


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(texts.HELP, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("test"))
async def cmd_test(message: Message, services: Services):
    """Демо-карточка в том виде, который выбран в /settings."""
    sub = services.db.subscriber(message.chat.id)
    await services.sender.send_vacancy(
        message.chat.id, demo_vacancy(),
        display=sub["display"] if sub else DEFAULT_DISPLAY,
    )


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
    extra = ""
    if stats.get("no_audience"):
        extra = (
            f"\nℹ️ Ещё {stats['no_audience']} обработал, но подписчиков этих разделов "
            "не нашлось — проверьте ⚙️ /settings."
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
                                    "failed", "send_failed")}),
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message, services: Services):
    paused = "⏸ выключен (/resume — включить)" if services.monitor.paused else "▶️ работает"
    fmt = (
        "Bubble/Rich/Классика — у каждого свой выбор в /settings"
        if services.cfg.use_widget and services.cfg.use_rich
        else ("Rich + классика" if services.cfg.use_rich else "классический HTML")
    )
    feeds = " · ".join(f.label for f in services.cfg.feeds)
    await message.answer(
        texts.STATUS_TMPL.format(
            paused=paused,
            interval=services.cfg.poll_minutes,
            fast_sec=int(services.cfg.fast_poll_sec),
            feeds=html_mod.escape(feeds),
            cookie=_fmt_cookie(services.client),
            rich=fmt,
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
        await cb.answer(f"Вид карточек: {texts.DISPLAY_LABELS_RU.get(display, display)}")
    else:
        await cb.answer("✅ Сохранено")


@router.message(Command("pause"))
async def cmd_pause(message: Message, services: Services):
    services.monitor.pause()
    await message.answer(texts.PAUSED)


@router.message(Command("resume"))
async def cmd_resume(message: Message, services: Services):
    services.monitor.resume()
    await message.answer(texts.RESUMED)


@router.message(Command("unsubscribe"))
async def cmd_unsub(message: Message, services: Services):
    services.db.remove_subscriber(message.chat.id)
    await message.answer(texts.UNSUBSCRIBED)


@router.message(Command("setcookie"))
async def cmd_setcookie(message: Message, command: CommandObject, services: Services):
    """Обновление cookie прямо в чате (только личка). Строка не сохраняется в логах."""
    if message.chat.type != "private":
        await message.answer("🔒 Cookie можно менять только в личном чате с ботом.")
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


@router.message()
async def fallback(message: Message):
    """Любое другое сообщение — подсказка."""
    if not await _ensure_private(message):
        return
    await message.answer(
        "🤖 Я понимаю только команды. Список — /help, проверить вакансии — /fresh, "
        "настроить рассылку — /settings.",
    )


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
