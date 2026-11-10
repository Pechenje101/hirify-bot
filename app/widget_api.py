"""Bot API 10.4 (Widgets) для aiogram 3.31 — методы, которых ещё нет в библиотеке.

aiogram 3.31 знает Bot API 10.1–10.3 (sendRichMessage и т.д.), а HTML-виджеты
появились в Bot API 10.4 (9 ноября 2026). aiogram умеет вызывать произвольные
методы: достаточно унаследоваться от TelegramMethod, указать __api_method__
и __returning__ — дальше сессия сама сериализует поля и сделает POST /sendWidget.

Спецификация (core.telegram.org/bots/api#sendwidget):
- html: 1–32768 байт UTF-8, ≤4096 байт после zlib-9 (проверяем в app/bubble.py);
- caption: 0–1024 символов — то, что видно в уведомлениях и старых клиентах;
- disable_notification — тихий режим работает как у обычных сообщений;
- reply_markup — инлайн-клавиатура под виджетом разрешена;
- allow_queries — здесь не нужен: карточка только показывает информацию,
  все действия — кнопки внутри (openTelegramLink/copyText) и инлайн-клавиатура.
"""
from __future__ import annotations

from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message


class SendWidget(TelegramMethod[Message]):
    """Отправка сообщения с HTML-виджетом (только личные чаты, Bot API 10.4+)."""

    __returning__ = Message
    __api_method__ = "sendWidget"

    chat_id: int
    html: str
    caption: str | None = None
    parse_mode: str | None = None
    show_caption_above_media: bool | None = None
    width: int | None = None
    height: int | None = None
    public_data: str | None = None
    chat_data: str | None = None
    allow_queries: bool | None = None
    is_shared: bool | None = None
    disable_notification: bool | None = None
    protect_content: bool | None = None
    reply_markup: InlineKeyboardMarkup | None = None
