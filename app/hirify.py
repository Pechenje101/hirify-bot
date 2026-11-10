"""HTTP-клиент hirify.me: фид вакансий и страницы вакансий (с cookie-авторизацией)."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import httpx

log = logging.getLogger("hirify.client")

# Пасть как обычный браузер — hirify за Cloudflare, важно отдавать реалистичные заголовки.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://hirify.me/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}


class HirifyError(Exception):
    """Общая ошибка при обращении к hirify.me."""


class HirifyNotFound(HirifyError):
    """Страница не существует (404/410) — повторять бессмысленно."""


class HirifyClient:
    """Асинхронный клиент с полем cookie, который можно менять на ходу."""

    def __init__(self, config):
        self.cfg = config
        self.cookie: str = config.hirify_cookie
        self._client: Optional[httpx.AsyncClient] = None
        self._last_request_ts = 0.0

    # ---------- жизненный цикл ----------

    async def open(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            headers=dict(BROWSER_HEADERS),
            timeout=self.cfg.request_timeout_sec,
            follow_redirects=True,
            # http2, если пакет h2 установлен
            http2=False,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def set_cookie(self, cookie: str) -> None:
        """Обновляем cookie на ходу (например, командой /setcookie)."""
        self.cookie = (cookie or "").strip()

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie)

    # ---------- запросы ----------

    async def _throttle(self) -> None:
        """Не долбим сайт подряд: пауза + случайный джиттер."""
        min_gap = self.cfg.fetch_delay_sec
        now = time.monotonic()
        elapsed = now - self._last_request_ts
        if elapsed < min_gap:
            await asyncio.sleep(min_gap - elapsed + random.uniform(0.0, 0.4))
        self._last_request_ts = time.monotonic()

    def _headers(self) -> dict:
        h = {}
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    async def _get(self, url: str) -> str:
        assert self._client is not None, "Клиент не открыт — вызовите open()"
        await self._throttle()
        last_err: Exception | None = None
        last_status: int | None = None
        for attempt in range(3):
            try:
                resp = await self._client.get(url, headers=self._headers())
                last_status = resp.status_code
                if resp.status_code in (404, 410):
                    # вакансия удалена — ретраить бессмысленно
                    raise HirifyNotFound(f"hirify вернул HTTP {resp.status_code} для {url}")
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    log.warning("hirify: 429, жду %.1fs", retry_after)
                    await asyncio.sleep(min(retry_after + 1.0, 60.0))
                    continue
                if resp.status_code in (502, 503, 504):
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                if resp.status_code != 200:
                    raise HirifyError(f"hirify вернул HTTP {resp.status_code} для {url}")
                return resp.text
            except httpx.HTTPError as e:  # network / timeout
                last_err = e
                await asyncio.sleep(1.5 * (attempt + 1))
        detail = f"HTTP {last_status}" if last_status is not None else last_err
        raise HirifyError(f"Не удалось загрузить {url}: {detail}")

    async def fetch_feed(self, feed, page: int = 1) -> str:
        """HTML страницы фида раздела feed с текущими фильтрами.

        feed — app.config.Feed (лендинг раздела: удалёнка / глобальная / crypto).
        """
        return await self._get(self.cfg.feed_url_for_page(feed, page))

    async def fetch_job(self, job_url: str) -> str:
        """HTML страницы вакансии /jobs/<id>-<slug>."""
        url = job_url if job_url.startswith("http") else self.cfg.hirify_base + job_url
        return await self._get(url)
