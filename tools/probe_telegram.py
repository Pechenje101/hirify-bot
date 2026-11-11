"""Живая проба Telegram Bot API: getMe → sendWidget → sendRichMessage.

Цель — проверить, принимает ли прод-сервер Telegram наш конвейер «пузыря»
(Bot API 10.4) и rich-сообщений (Bot API 10.1), и поймать точный текст ошибки.

Что делает:
  1. getMe — токен живой? печатает @username бота (это не секрет).
  2. sendWidget СЫРЫМ HTTP-запросом (как их делает сам Telegram-клиент)
     и ЧЕРЕЗ aiogram — ровно тот путь, что в app/sender.py::_send_widget.
     Отправляем в chat_id=1: сообщение НИКУДА не уйдёт («chat not found»),
     но сервер успевает провалидировать метод, параметры и html — этого
     достаточно, чтобы понять, почему бот в проде откатывается на классику.
  3. То же с минимальным html (чтобы отделить «метод не существует» от
     «наш html не нравится»).
  4. sendRichMessage (InputRichMessage) с rich-html демо-карточки.

Токен берётся из BOT_TOKEN и НИГДЕ не печатается: в лог уходят только
названия методов, HTTP-коды и текст ответа сервера (ошибок).

Запуск: GitHub Actions (probe-tg.yml, секрет BOT_TOKEN), либо локально
с переменной окружения BOT_TOKEN.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from app.bubble import assert_widget_fits, render_bubble_caption, render_bubble_html  # noqa: E402
from app.cards import demo_vacancy, render_rich_html  # noqa: E402
from app.widget_api import SendWidget  # noqa: E402

DEMO = demo_vacancy()

# Несуществующий чат: сервер отвечает «chat not found», ничего не отправляя.
# При этом parse-стадия метода и параметров уже пройдена — что нам и нужно.
PROBE_CHAT_ID = 1


def _mask(url: str) -> str:
    """Прячем токен из URL, если он случайно попадёт в лог."""
    import re

    return re.sub(r"bot\d+:[^/]+", "bot<token>", url)


async def raw_call(token: str, method: str, fields: dict) -> None:
    """Сырой POST как из доки: api.telegram.org/bot<token>/<method>."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = {k: str(v) for k, v in fields.items() if v is not None}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=data) as r:
                body = (await r.text())[:400]
                print(f"\n[raw] {method} → HTTP {r.status}\n{_mask(body)}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[raw] {method} → сбой транспорта: {type(e).__name__}: {e}")


async def aiogram_call(token: str) -> None:
    """Ровно путь app/sender.py: Bot + кастомный SendWidget из app/widget_api."""
    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError

    bot = Bot(token=token)
    html = render_bubble_html(DEMO)
    try:
        await bot(SendWidget(
            chat_id=PROBE_CHAT_ID,
            html=html,
            caption=render_bubble_caption(DEMO),
            disable_notification=False,
            reply_markup=None,
        ))
        print("\n[aiogram] sendWidget → неожиданный успех (сообщения не должно было уйти)")
    except TelegramAPIError as e:
        print(f"\n[aiogram] sendWidget → {type(e).__name__} (HTTP {getattr(e, 'status_code', '?')}):\n"
              f"        {e}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[aiogram] sendWidget → {type(e).__name__}: {e}")
    finally:
        await bot.session.close()


async def main() -> None:
    import os

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        print("❌ BOT_TOKEN не задан — проба невозможна")
        return

    html = render_bubble_html(DEMO)
    raw, comp = assert_widget_fits(html)
    print(f"Пузырь демо-вакансии: {raw} байт raw / {comp} байт zlib-9 "
          f"(лимиты 32768/4096 — {'OK' if comp <= 4096 else 'ПЕРЕПОЛНЕНИЕ'})")

    # 1) getMe — токен живой?
    await raw_call(token, "getMe", {})
    # 2) sendWidget сырым запросом: наш полный html-пузырь
    await raw_call(token, "sendWidget",
                   {"chat_id": PROBE_CHAT_ID, "html": html,
                    "caption": render_bubble_caption(DEMO)})
    # 3) sendWidget с минимальным html
    await raw_call(token, "sendWidget",
                   {"chat_id": PROBE_CHAT_ID, "html": "<b>hi</b>", "caption": "probe"})
    # 4) sendRichMessage (Bot API 10.1) с rich-html карточки
    await raw_call(token, "sendRichMessage",
                   {"chat_id": PROBE_CHAT_ID,
                    "rich_message": render_rich_html(DEMO, limit=32768)})
    # 5) тот же sendWidget через aiogram — путь приложения целиком
    await aiogram_call(token)


if __name__ == "__main__":
    asyncio.run(main())
