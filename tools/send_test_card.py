"""Разовая тест-доставка: карточка вакансии с сайта hirify.me владельцу.

Проходит ЖИВОЙ конвейер в одном проходе, но безопасно (без аккаунта):
анонимный лендинг вакансии (SEO-JSON-LD, cookie не нужен) -> карточка теми
же модулями, что и рассылка (VacancySender) -> в чат владельца.

Кнопка «Показать контакты» — рабочая: нажатие обслужит ЖИВОЙ ран бота
(вакансия уже есть в БД), контакт допишется в ту же карточку — это и есть
демо кнопочного режима CONTACTS_MODE=button.

Запуск (секреты приходят из окружения окружения Actions, НЕ из репо):
    BOT_TOKEN=... ADMIN_CHAT_ID=... python tools/send_test_card.py [slug]

Без slug берётся свежая крипто-вакансия из БД. БД открывается read-only:
скрипт ничего не пишет и не коммитит.
"""
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import FEED_PRESETS, Config
from app.hirify import HirifyClient
from app.parsing import parse_job_page
from app.sender import VacancySender

DEFAULT_SLUG_SQL = (
    "SELECT job_id, slug, url FROM jobs "
    "WHERE slug LIKE '%crypto%' OR slug LIKE '%igaming%' "
    "ORDER BY first_seen DESC LIMIT 1"
)


async def main() -> int:
    cfg = Config()
    if not cfg.bot_token or not os.getenv("ADMIN_CHAT_ID"):
        print("Нужны BOT_TOKEN и ADMIN_CHAT_ID в окружении")
        return 2

    db = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    if len(sys.argv) > 1:
        row = db.execute(
            "SELECT job_id, slug, url FROM jobs WHERE slug = ?", (sys.argv[1],)
        ).fetchone()
    else:
        row = db.execute(DEFAULT_SLUG_SQL).fetchone()
    db.close()
    if row is None:
        print("Вакансия не найдена в БД (укажите slug существующей)")
        return 2

    # анонимный путь: клиент без cookie — аккаунт в тесте не участвует
    client = HirifyClient(cfg)
    await client.open()
    landing = await client.fetch_job(row["url"])
    await client.close()
    v = parse_job_page(landing, row["url"])
    feed = FEED_PRESETS["crypto"]
    v.feed_key, v.feed_label = feed.key, feed.label
    if not v.has_direct_contact:
        v.contacts_by_button = True

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    sender = VacancySender(bot, cfg)
    sent_with, message_id = await sender.send_vacancy(
        int(os.getenv("ADMIN_CHAT_ID", "")), v, want_message=True
    )
    await bot.session.close()

    how = "контакт в карточке (виден анонимно)" if v.has_direct_contact else "контакт по кнопке"
    print(f"OK: {row['slug']} (job {row['job_id']}) доставлено как {sent_with}, "
          f"message_id={message_id}; {how}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
