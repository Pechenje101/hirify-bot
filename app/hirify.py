"""HTTP-клиент hirify.me: фид вакансий (HTML) + JSON-API вакансий и контактов
(с cookie-авторизацией). API живёт на отдельном домене api.hirify.me и
использует Laravel Sanctum: POST-запросам нужен заголовок X-XSRF-TOKEN
из cookie XSRF-TOKEN, а также Origin/Referer нашего сайта.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional
from urllib.parse import unquote

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


class HirifyAuthError(HirifyError):
    """Сессия (cookie) не работает: 401/419 — надо перелогиниться."""


class HirifyRestrictedError(HirifyError):
    """Доступ к контактам ограничен финально (403: премиум/нет попыток) —
    ретраить бессмысленно."""


class HirifyAutomationError(HirifyError):
    """Сайт заподозрил автоматизацию (403 automation_detected) — временно:
    лучше притормозить и повторить позже."""


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

    def _api_headers(self, post: bool = False) -> dict:
        """Заголовки для JSON-API api.hirify.me (Laravel Sanctum).

        POST-эндпоинтам нужен X-XSRF-TOKEN — расшифрованное значение cookie
        XSRF-TOKEN (как делает фронтенд сайта), иначе сервер ответит 419.
        """
        h = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": "https://hirify.me/jobs/",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if self.cookie:
            h["Cookie"] = self.cookie
            if post:
                for part in self.cookie.split(";"):
                    k, _, val = part.strip().partition("=")
                    if k == "XSRF-TOKEN":
                        h["X-XSRF-TOKEN"] = unquote(val)
                        break
        return h

    async def _api_call(self, method: str, path: str) -> dict:
        """GET/POST к api.hirify.me с общими ретраями и обработкой статусов."""
        assert self._client is not None, "Клиент не открыт — вызовите open()"
        await self._throttle()
        url = self.cfg.hirify_api_base + path
        last_status: int | None = None
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.request(
                    method, url, headers=self._api_headers(post=(method == "POST")))
                last_status = resp.status_code
                if resp.status_code in (404, 410):
                    raise HirifyNotFound(f"hirify вернул HTTP {resp.status_code} для {url}")
                if resp.status_code in (401, 419):
                    # сессия не работает: cookie истёк/отозван
                    raise HirifyAuthError(f"сессия не принята (HTTP {resp.status_code})")
                if resp.status_code == 403:
                    # различаем финальные и временные причины отказа:
                    #  · премиум/лимит попыток («No free attempts remaining») —
                    #    финально для этой вакансии;
                    #  · «automation_detected» — временный флаг аккаунта.
                    body = (resp.text or "")[:200]
                    if "automation_detected" in body or "automated" in body:
                        raise HirifyAutomationError(
                            f"сайт заподозрил автоматизацию (HTTP 403): {body}")
                    raise HirifyRestrictedError(
                        f"доступ к контактам ограничен (HTTP 403): {body}")
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    log.warning("hirify api: 429, жду %.1fs", retry_after)
                    await asyncio.sleep(min(retry_after + 1.0, 60.0))
                    continue
                if resp.status_code in (502, 503, 504):
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                if resp.status_code != 200:
                    raise HirifyError(
                        f"hirify вернул HTTP {resp.status_code} для {url}: "
                        f"{resp.text[:120]}")
                try:
                    return resp.json()
                except ValueError as e:
                    raise HirifyError(f"api.hirify.me вернул не JSON: {e}") from e
            except httpx.HTTPError as e:  # network / timeout
                last_err = e
                await asyncio.sleep(1.5 * (attempt + 1))
        detail = f"HTTP {last_status}" if last_status is not None else last_err
        raise HirifyError(f"Не удалось загрузить {url}: {detail}")

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
        """HTML страницы вакансии /jobs/<id>-<slug> (legacy-путь, живёт для
        инструментов диагностики; монитор использует fetch_vacancy/fetch_contacts)."""
        url = job_url if job_url.startswith("http") else self.cfg.hirify_base + job_url
        return await self._get(url)

    # ---------- JSON-API (api.hirify.me) ----------

    async def fetch_vacancy(self, slug: str) -> dict:
        """Данные вакансии: GET /api/vacancies/<slug> (требует сессию)."""
        return await self._api_call("GET", f"/api/vacancies/{slug}")

    async def fetch_contacts(self, slug: str) -> dict:
        """Прямые контакты вакансии: POST /api/vacancies/<slug>/contacts.

        Возвращает {contacts: [{type, value, short_code}], access_type, …}.
        Значение value — юзернейм рекрутера или t.me-ссылка (пост в канале).
        """
        return await self._api_call("POST", f"/api/vacancies/{slug}/contacts")

    async def fetch_feed_api(self, feed, page: int = 1) -> list[dict]:
        """Фид раздела из JSON-API: только вакансии с прямыми TG-контактами.

        GET /api/vacancies?{feed.api_params}&{feed_params}&page=N — фильтры
        работают на сервере (в отличие от query лендинга HTML!). Возвращает
        список элементов-«карточек» (id, slug, source, work_format, tldr, …;
        полное описание — отдельным запросом fetch_vacancy).
        """
        params = "&".join(
            p for p in self.cfg.feed_params.split("&")
            if p and not p.startswith("page=")
        )
        q = "&".join(x for x in (feed.api_params, params, f"page={max(1, page)}") if x)
        data = await self._api_call("GET", f"/api/vacancies?{q}")
        if isinstance(data, dict):
            items = data.get("data") or []
            return items if isinstance(items, list) else []
        return data if isinstance(data, list) else []
