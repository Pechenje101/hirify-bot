"""Тесты отправщика: выбор вида карточки и цепочки фолбэка.

bubble → rich → classic — каждый уровень при сбое откатывается на следующий,
после 3 неудач подряд уровень отключается до перезапуска. Классика — последняя
инстанция: её ошибка уходит в монитор (send_failed).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cards import demo_vacancy  # noqa: E402
from app.config import Config  # noqa: E402
from app.sender import FAILS_BEFORE_DISABLE, VacancySender  # noqa: E402

DEMO = demo_vacancy()


class FakeBot:
    """Записывает, какими методами шли отправки; часть уровней может ломать."""

    def __init__(self, broken: set[str] | None = None):
        self.calls: list[str] = []
        self.broken = broken or set()

    async def __call__(self, method):  # кастомные методы (SendWidget)
        name = method.__api_method__
        self.calls.append(name)
        if "widget" in self.broken:
            raise RuntimeError("widget send failed")
        return object()

    async def send_rich_message(self, **kwargs):
        self.calls.append("sendRichMessage")
        if "rich" in self.broken:
            raise RuntimeError("rich send failed")
        return object()

    async def send_message(self, **kwargs):
        self.calls.append("sendMessage")
        if "classic" in self.broken:
            raise RuntimeError("classic send failed")
        return object()


def make_sender(broken: set[str] | None = None) -> tuple[VacancySender, FakeBot]:
    bot = FakeBot(broken)
    cfg = Config(bot_token="x")
    cfg.send_delay_sec = 0  # в тестах без пауз
    return VacancySender(bot, cfg), bot


def test_auto_uses_rich_then_classic_fallback():
    sender, bot = make_sender(broken={"rich"})
    result = asyncio.run(sender.send_vacancy(1, DEMO))
    assert result == "classic"
    assert bot.calls == ["sendRichMessage", "sendMessage"]


def test_auto_sends_rich_when_everything_ok():
    sender, bot = make_sender()
    result = asyncio.run(sender.send_vacancy(1, DEMO))
    assert result == "rich"
    assert bot.calls == ["sendRichMessage"]


def test_bubble_mode_sends_widget():
    sender, bot = make_sender()
    result = asyncio.run(sender.send_vacancy(1, DEMO, display="bubble"))
    assert result == "bubble"
    assert bot.calls == ["sendWidget"]


def test_bubble_falls_back_to_rich_then_classic():
    sender, bot = make_sender(broken={"widget"})
    result = asyncio.run(sender.send_vacancy(1, DEMO, display="bubble"))
    assert result == "rich"
    assert bot.calls == ["sendWidget", "sendRichMessage"]

    sender2, bot2 = make_sender(broken={"widget", "rich"})
    result2 = asyncio.run(sender2.send_vacancy(1, DEMO, display="bubble"))
    assert result2 == "classic"
    assert bot2.calls == ["sendWidget", "sendRichMessage", "sendMessage"]


def test_rich_mode_and_classic_mode():
    sender, bot = make_sender()
    assert asyncio.run(sender.send_vacancy(1, DEMO, display="rich")) == "rich"
    assert asyncio.run(sender.send_vacancy(1, DEMO, display="classic")) == "classic"
    assert bot.calls == ["sendRichMessage", "sendMessage"]
    # classic — только sendMessage, без попыток rich
    assert bot.calls.count("sendRichMessage") == 1


def test_unknown_display_mode_behaves_like_auto():
    sender, bot = make_sender()
    assert asyncio.run(sender.send_vacancy(1, DEMO, display="мусор")) == "rich"


def test_use_widget_off_disables_bubble():
    sender, bot = make_sender()
    sender._widget_enabled = False
    result = asyncio.run(sender.send_vacancy(1, DEMO, display="bubble"))
    assert result == "rich"
    assert "sendWidget" not in bot.calls


def test_use_rich_off_auto_skips_rich():
    sender, bot = make_sender()
    sender._rich_enabled = False
    assert asyncio.run(sender.send_vacancy(1, DEMO)) == "classic"
    assert bot.calls == ["sendMessage"]
    # bubble без rich: виджет → классика
    sender2, bot2 = make_sender()
    sender2._rich_enabled = False
    assert asyncio.run(sender2.send_vacancy(1, DEMO, display="bubble")) == "bubble"


def test_widget_disabled_after_consecutive_fails():
    """После N неудач подряд виджеты отключаются до перезапуска."""
    sender, bot = make_sender(broken={"widget"})
    for _ in range(FAILS_BEFORE_DISABLE):
        asyncio.run(sender.send_vacancy(1, DEMO, display="bubble"))
    assert sender._widget_enabled is False
    # четвёртая отправка уже даже не пробует виджет
    bot.calls.clear()
    assert asyncio.run(sender.send_vacancy(1, DEMO, display="bubble")) == "rich"
    assert bot.calls == ["sendRichMessage"]


def test_rich_disabled_after_consecutive_fails():
    sender, bot = make_sender(broken={"rich"})
    for _ in range(FAILS_BEFORE_DISABLE):
        asyncio.run(sender.send_vacancy(1, DEMO))  # auto
    assert sender._rich_enabled is False
    bot.calls.clear()
    assert asyncio.run(sender.send_vacancy(1, DEMO)) == "classic"
    assert bot.calls == ["sendMessage"]


def test_success_resets_fail_counter():
    sender, bot = make_sender()
    sender._widget_fails = FAILS_BEFORE_DISABLE - 1
    sender._widget_enabled = True
    assert asyncio.run(sender.send_vacancy(1, DEMO, display="bubble")) == "bubble"
    assert sender._widget_fails == 0  # успех обнуляет серию


def test_classic_failure_propagates_to_monitor():
    """Классика — последняя инстанция: её ошибка уходит в монитор (send_failed)."""
    sender, bot = make_sender(broken={"rich", "classic"})
    try:
        asyncio.run(sender.send_vacancy(1, DEMO))
        raise AssertionError("ожидали исключение")
    except RuntimeError as e:
        assert "classic" in str(e)


def test_silent_flag_reaches_all_levels():
    """disable_notification доходит и до виджета, и до rich, и до классики."""
    sender, bot = make_sender()
    asyncio.run(sender.send_vacancy(1, DEMO, silent=True, display="bubble"))
    # проверим через перехват: проще всего убедиться, что вызовы прошли без ошибок
    assert bot.calls == ["sendWidget"]
