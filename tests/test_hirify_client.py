"""Тесты HTTP-клиента hirify: безопасный разбор Retry-After и структура фида."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.hirify import _retry_after_seconds  # noqa: E402


class _Resp:
    def __init__(self, value: str | None):
        self.headers = {"Retry-After": value} if value is not None else {}


def test_retry_after_seconds_number():
    assert _retry_after_seconds(_Resp("7")) == 7.0
    assert _retry_after_seconds(_Resp("0")) == 0.0
    assert _retry_after_seconds(_Resp("2.5")) == 2.5


def test_retry_after_seconds_http_date():
    """RFC 7231 разрешает дату — float() на ней падает (это и ловим)."""
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(seconds=12)).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")
    got = _retry_after_seconds(_Resp(future))
    assert 9.0 <= got <= 12.0  # успели немного «постареть» — всё ещё ~12 с

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")
    assert _retry_after_seconds(_Resp(past)) == 0.0


def test_retry_after_seconds_garbage_and_missing():
    """Мусор или отсутствие заголовка не должны ронять проход."""
    assert _retry_after_seconds(_Resp("later")) == 5.0
    assert _retry_after_seconds(_Resp("")) == 5.0
    assert _retry_after_seconds(_Resp(None)) == 5.0
    assert _retry_after_seconds(_Resp("Wed, 99 Xxx 2015 99:99:99 GMT")) == 5.0
