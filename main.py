"""Точка входа: aiogram-бот + фоновый мониторинг hirify.me.

Запуск:  python main.py
Требования: Python 3.10+, зависимости из requirements.txt
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app import handlers
from app.config import load_config
from app.db import Database
from app.hirify import HirifyClient
from app.monitor import Monitor
from app.sender import VacancySender
from app.services import Services

log = logging.getLogger("hirify.main")

BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск и подписка на вакансии"),
    BotCommand(command="fresh", description="Проверить вакансии сейчас"),
    BotCommand(command="digest", description="Дайджест последних вакансий"),
    BotCommand(command="pending", description="Очередь модерации канала"),
    BotCommand(command="week", description="Недельная аналитика канала"),
    BotCommand(command="settings", description="Разделы, вид карточек, тихий режим"),
    BotCommand(command="test", description="Демо-карточка вакансии"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="status", description="Статус и настройки"),
    BotCommand(command="pause", description="Пауза автомониторинга"),
    BotCommand(command="resume", description="Возобновить автомониторинг"),
    BotCommand(command="setcookie", description="Обновить cookie hirify.me"),
    BotCommand(command="unsubscribe", description="Отписаться"),
    BotCommand(command="help", description="Помощь"),
]


def setup_logging(cfg) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO), format=fmt)
    # чуть тише шумные логгеры
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run() -> None:
    cfg = load_config()
    setup_logging(cfg)

    if not cfg.bot_token:
        print("❌ BOT_TOKEN не задан. Скопируйте .env.example в .env и впишите токен от @BotFather.")
        sys.exit(1)

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    db = Database(cfg.db_path)
    client = HirifyClient(cfg)
    sender = VacancySender(bot, cfg)
    monitor = Monitor(client, db, sender, cfg)

    # cookie из /setcookie живёт в БД и переживает перезапуск
    saved_cookie = db.get_setting("hirify_cookie", "")
    if saved_cookie:
        client.set_cookie(saved_cookie)

    services = Services(cfg=cfg, db=db, client=client, sender=sender, monitor=monitor)
    dp["services"] = services  # инжектится в хендлеры по имени аргумента `services`
    dp.include_router(handlers.router)

    await client.open()
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось установить меню команд: %s", e)
    # фоновый мониторинг + приём команд
    monitor_task = asyncio.create_task(monitor.run_forever())
    log.info("Бот запущен. Cookie: %s", "есть" if client.has_cookie else "нет (см. /help)")
    if not client.has_cookie:
        log.warning("HIRIFY_COOKIE пуст: без авторизации на hirify.me контакты не видны!")

    polling_task = asyncio.create_task(
        # drop_pending_updates=False в режиме Actions: команды, присланные
        # в зазор между ранами, обрабатываются новым раном, а не теряются
        dp.start_polling(bot, drop_pending_updates=cfg.drop_pending_updates),
    )

    async def _run_timer() -> None:
        """RUN_MAX_MINUTES (режим GitHub Actions): плавная остановка до лимита job'а."""
        await asyncio.sleep(cfg.run_max_minutes * 60)
        log.info(
            "RUN_MAX_MINUTES=%.0f мин истёк — плавная остановка (перезапустит воркфлоу)",
            cfg.run_max_minutes,
        )
        polling_task.cancel()

    timer_task = asyncio.create_task(_run_timer()) if cfg.run_max_minutes > 0 else None
    try:
        await polling_task
    except asyncio.CancelledError:
        if not polling_task.cancelled():
            raise  # отменили нас самих (Ctrl+C) — пропускаем наверх
        log.info("Поллинг остановлен по таймеру — штатная эстафета ранов")
    finally:
        if timer_task is not None:
            timer_task.cancel()
        monitor.stop()
        monitor_task.cancel()
        try:
            await monitor_task  # даём фоновому циклу корректно завершиться
        except asyncio.CancelledError:
            pass
        await client.close()
        await monitor.aclose()  # сессия ТГ-каналов
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        print("\n👋 Бот остановлен.")
