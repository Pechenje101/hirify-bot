"""Отправка карточек в Telegram по выбранному подписчиком виду.

Виды карточек (персональная настройка display из /settings, см. db.DISPLAY_MODES):
- "bubble"  — HTML-виджет (Bot API 10.4, Telegram 13.2+): настоящий CSS — тени,
  панели, кнопки внутри виджета. Фолбэк: rich → классика.
- "rich"    — Rich Message (Bot API 10.1+): красивая HTML-разметка.
  Фолбэк: классика.
- "classic" — обычный parse_mode=HTML + инлайн-клавиатура, работает везде.
- "auto"    — как раньше: rich → классика.

Каждый уровень при сбое откатывается на следующий, поэтому карточка дойдёт
в любом случае. Если rich/виджеты не проходят несколько раз подряд — уровень
отключается до перезапуска (не тратим время на заведомый сбой).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from .bubble import assert_widget_fits, render_bubble_caption, render_bubble_html
from .cards import inline_keyboard, render_classic_html, render_rich_html
from .config import Config
from .db import DEFAULT_DISPLAY
from .parsing import Vacancy

log = logging.getLogger("hirify.sender")

# Rich/виджеты подряд не прошли N раз — значит, тут они не поддерживаются:
# до перезапуска шлём следующую ступень, не тратя время на заведомый сбой
FAILS_BEFORE_DISABLE = 3


class VacancySender:
    def __init__(self, bot: Bot, config: Config):
        self.bot = bot
        self.cfg = config
        self._rich_enabled = config.use_rich
        self._widget_enabled = config.use_widget
        self._rich_fails = 0    # подряд неудачных rich-отправок
        self._widget_fails = 0  # подряд неудачных отправок виджетом

    # ---------------------------------------------------------------- API

    async def send_vacancy(
        self,
        chat_id: int,
        v: Vacancy,
        *,
        silent: bool = False,
        display: str = DEFAULT_DISPLAY,
    ) -> str:
        """Отправляет карточку выбранным видом. Возвращает, чем отправили:
        'bubble' | 'rich' | 'classic'.

        silent=True — доставить без звука (disable_notification, настройка
        подписчика из /settings).
        """
        sent_with = "classic"
        plan = self._plan(display)
        for i, (mode, sender) in enumerate(plan):
            try:
                await sender(chat_id, v, silent)
                sent_with = mode
                if mode == "rich":
                    self._rich_fails = 0
                elif mode == "bubble":
                    self._widget_fails = 0
                break
            except Exception as e:  # noqa: BLE001 — любой сбой → следующая ступень
                self._register_fail(mode, e)
                if i == len(plan) - 1:
                    raise  # классика — последняя инстанция: ошибка уходит в монитор
        await asyncio.sleep(self.cfg.send_delay_sec)
        # вид доставки в лог: сразу видно, уходят ли выбравшим «Пузырь» виджеты,
        # или они молча откатываются к rich/классике (и почему — строками выше)
        log.info("job %s → чат %s: доставлено видом %s (выбрано %s)",
                 v.job_id, chat_id, sent_with, display)
        return sent_with

    # ------------------------------------------------------------ план

    def _plan(self, display: str) -> list[tuple[str, object]]:
        """Цепочка попыток для выбранного вида: bubble → rich → classic."""
        if display == "classic":
            return [("classic", self._send_classic)]
        steps: list[tuple[str, object]] = []
        if display == "bubble" and self._widget_enabled:
            steps.append(("bubble", self._send_widget))
        if self._rich_enabled:
            steps.append(("rich", self._send_rich))
        steps.append(("classic", self._send_classic))
        return steps

    def _register_fail(self, mode: str, exc: Exception) -> None:
        if mode == "rich":
            self._rich_fails += 1
            if self._rich_fails >= FAILS_BEFORE_DISABLE:
                self._rich_enabled = False
                log.warning(
                    "Rich Message не прошёл %d раз подряд (%s) — до перезапуска шлём классику",
                    self._rich_fails, exc,
                )
            else:
                log.info("Rich Message не прошёл (%s), откат на следующий вид", exc)
        elif mode == "bubble":
            self._widget_fails += 1
            if self._widget_fails >= FAILS_BEFORE_DISABLE:
                self._widget_enabled = False
                log.warning(
                    "Виджет не прошёл %d раз подряд (%s) — до перезапуска шлём rich/классику",
                    self._widget_fails, exc,
                )
            else:
                # виджет — главная «фишка»: каждое откатывание видно в логах WARNING,
                # чтобы причина (размер html, клиент без поддержки, лимиты) не пряталась
                log.warning("Виджет не прошёл (%s), откат на следующий вид", exc)
        else:
            log.debug("Классическая отправка не прошла: %s", exc)

    # ------------------------------------------------------------ bubble

    async def _send_widget(self, chat_id: int, v: Vacancy, silent: bool) -> None:
        from .widget_api import SendWidget

        html = render_bubble_html(v)
        assert_widget_fits(html)  # лучше поймать переполнение сами, чем словить 4xx
        # Кнопки «Написать/Копировать» уже внутри виджета (openTelegramLink /
        # copyText). Для контакта вида tg://user?id= виджет открывать чат не
        # умеет — под виджет уходит обычная инлайн-клавиатура с кнопкой-URL.
        markup = None
        if v.primary_contact and v.primary_contact.kind == "user_id":
            markup = inline_keyboard(v)
        await self._with_retry(
            lambda: self.bot(
                SendWidget(
                    chat_id=chat_id,
                    html=html,
                    caption=render_bubble_caption(v),
                    disable_notification=silent,
                    reply_markup=markup,
                )
            )
        )

    # ------------------------------------------------------------ rich

    async def _send_rich(self, chat_id: int, v: Vacancy, silent: bool) -> None:
        from aiogram.types import InputRichMessage

        # лимит Bot API 10.1+ — 32768 символов html: описание вкладываем целиком
        # (автобюджет внутри рендера), тизер + сворачиваемый полный текст
        html = render_rich_html(v)
        # ВАЖНО: без reply_markup — кнопки уже внутри rich-разметки (tg-button),
        # иначе у сообщения были бы дублирующиеся ряды кнопок.
        await self._with_retry(
            lambda: self.bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(html=html),
                disable_notification=silent,
            )
        )

    # ------------------------------------------------------------ классика

    async def _send_classic(self, chat_id: int, v: Vacancy, silent: bool) -> None:
        # автобюджет под 4096 видимых символов: полное описание в expandable-цитате
        text = render_classic_html(v)
        await self._with_retry(
            lambda: self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                disable_notification=silent,
                reply_markup=inline_keyboard(v),
            )
        )

    # ------------------------------------------------------------ служебное

    async def send_text(self, chat_id: int, html: str) -> None:
        await self._with_retry(
            lambda: self.bot.send_message(
                chat_id=chat_id,
                text=html,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        )

    async def _with_retry(self, call, attempts: int = 3):
        """Флуд-контроль Telegram: ждём Retry-After и повторяем (до attempts раз)."""
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return await call()
            except TelegramRetryAfter as e:
                last_exc = e
                log.warning("Flood-контроль: пауза %.1f c (попытка %d/%d)",
                            e.retry_after, i + 1, attempts)
                await asyncio.sleep(e.retry_after + 1)
        raise last_exc if last_exc else RuntimeError("_with_retry: попытки исчерпаны")
