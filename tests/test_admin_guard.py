"""Тесты гарда критичных команд: ADMIN_CHAT_ID.
Без гарда любой пользователь, нашедший бота, мог бы подменить cookie
(/setcookie) или остановить мониторинг для всех (/pause)."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.handlers import _is_admin  # noqa: E402


def _msg(chat_id: int):
    """Минимальный «Message»: _is_admin читает только message.chat.id."""
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id))


def test_admin_guard_off_by_default():
    """ADMIN_CHAT_ID не задан (личный бот) — пропускаем всех, как раньше."""
    cfg = Config(bot_token="x")  # admin_chat_id = 0
    assert cfg.admin_chat_id == 0
    assert _is_admin(cfg, _msg(12345)) is True


def test_admin_guard_rejects_stranger():
    cfg = Config(bot_token="x")
    cfg.admin_chat_id = 42
    assert _is_admin(cfg, _msg(42)) is True       # владелец
    assert _is_admin(cfg, _msg(43)) is False      # чужой личный чат
    assert _is_admin(cfg, _msg(-100123)) is False # группа
