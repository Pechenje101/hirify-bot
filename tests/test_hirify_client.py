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


# ---------------------------------------------------- анти-флаг: гигиена аккаунта

import asyncio  # noqa: E402

from app.config import Config, FEED_PRESETS  # noqa: E402
from app.hirify import HirifyClient, _parse_gap  # noqa: E402

COOKIE = "hirifyme_session=abc; remember_web=xyz; XSRF-TOKEN=def%3D"


def _client() -> HirifyClient:
    cfg = Config(bot_token="x", hirify_cookie=COOKIE)
    cfg.fetch_delay_sec = 0.0
    cfg.vacancy_gap_sec = "0,0"
    cfg.contacts_gap_sec = "0,0"
    return HirifyClient(cfg)


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self.text = "{}"
        self.headers = {}
        self._payload = payload

    def json(self):
        return self._payload


def test_parse_gap_variants():
    assert _parse_gap("6,14", (1.0, 2.0)) == (6.0, 14.0)
    assert _parse_gap("6-14", (1.0, 2.0)) == (6.0, 14.0)
    assert _parse_gap("8", (1.0, 2.0)) == (8.0, 8.0)
    assert _parse_gap("", (1.0, 2.0)) == (1.0, 2.0)
    assert _parse_gap("мусор", (1.0, 2.0)) == (1.0, 2.0)
    assert _parse_gap("-2,9", (1.0, 2.0)) == (0.0, 9.0)   # отрицательные → 0


def test_gaps_parsed_from_config():
    c = _client()
    assert c.gaps["feed"] == (0.0, 0.4)
    assert c.gaps["vacancy"] == (0.0, 0.0)
    assert c.gaps["contacts"] == (0.0, 0.0)


def test_feed_api_is_anonymous_but_vacancy_and_contacts_are_not():
    """Фид — анонимно (поллинг не привязан к аккаунту), данные вакансии и
    контакты — от имени аккаунта (cookie + XSRF на POST)."""
    c = _client()
    captured: list[tuple[str, dict]] = []

    async def fake_transport(method, url, headers):
        captured.append((url, dict(headers)))
        if "vacancies?" in url:
            return _FakeResp({"data": []})
        if url.endswith("/contacts"):
            return _FakeResp({"contacts": []})
        return _FakeResp({"id": "1", "slug": "x"})

    c._transport = fake_transport  # подмена транспорта — сети нет

    asyncio.run(c.fetch_feed_api(FEED_PRESETS["remote"], 1))
    asyncio.run(c.fetch_vacancy("101-x"))
    asyncio.run(c.fetch_contacts("101-x"))

    feed_url, feed_h = captured[0]
    assert "/api/vacancies?" in feed_url
    assert "Cookie" not in feed_h and "X-XSRF-TOKEN" not in feed_h

    _, vac_h = captured[1]
    assert vac_h["Cookie"] == COOKIE

    _, post_h = captured[2]
    assert post_h["Cookie"] == COOKIE
    assert post_h["X-XSRF-TOKEN"] == "def="   # unquote(%3D) → '='


def test_open_prefers_curl_cffi_impersonation():
    """Основной двигатель — curl_cffi с имперсонацией Chrome (TLS как у браузера)."""
    from app import hirify as h

    if not h.HAS_CURL_CFFI:
        return  # окружение без curl_cffi — фолбэк на httpx тоже валиден
    c = _client()
    asyncio.run(c.open())
    try:
        assert c.engine.startswith("curl_cffi(")
        assert c._curl is not None and c._httpx is None
    finally:
        asyncio.run(c.close())
