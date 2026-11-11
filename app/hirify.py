"""HTTP-клиент hirify.me: фид вакансий (HTML) + JSON-API вакансий и контактов.

Двигатель запросов — curl_cffi с имперсонацией Chrome (настоящий TLS-отпечаток
ClientHello, HTTP/2/3 и порядок заголовков браузера): httpx говорил
HTTP/1.1 с «питоновским» TLS, тогда как User-Agent честно представлялся
Chrome — такое противоречие отлично видно в логах nginx и типовых
детекторах автоматизации. Если curl_cffi не установлен — откат на httpx
(как раньше).

Гигиена аккаунта (анти-флаг automation_detected):
  · фиды читаем АНОНИМНО, без cookie — круглосуточный поллинг не привязан
    к аккаунту (ограничение у hirify аккаунтное, а не сессионное);
  · cookie тратим только на «дорогие» запросы: данные вакансии и контакты;
  · Set-Cookie от сервера не копим (jar чист после каждого ответа) —
    личность запроса определяется только нашим явным заголовком, гостевые
    сессии не обрастают;
  · паузы между запросами — «человеческие»: лента листается быстро,
    вакансия читается десяток секунд, контакты открываются не мгновенно.

API живёт на отдельном домене api.hirify.me (nginx, HTTP/2+) и использует
Laravel Sanctum: POST-запросам нужен заголовок X-XSRF-TOKEN из cookie
XSRF-TOKEN, а также Origin/Referer нашего сайта.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import unquote

import httpx

log = logging.getLogger("hirify.client")

# curl_cffi — основной двигатель (имперсонация Chrome). Может отсутствовать
# в окружении — тогда честно откатываемся на httpx.
try:
    from curl_cffi import CurlError as CurlCffiError
    from curl_cffi.requests import AsyncSession as CurlAsyncSession

    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover - зависит от окружения
    CurlCffiError = None  # type: ignore[assignment,misc]
    CurlAsyncSession = None  # type: ignore[assignment,misc]
    HAS_CURL_CFFI = False

# Ошибки транспорта, после которых имеет смысл подождать и повторить.
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError, asyncio.TimeoutError,
) + ((CurlCffiError,) if HAS_CURL_CFFI else ())

# Пасть как обычный браузер — для httpx-фолбэка (в curl_cffi-режиме UA и
# клиентские хинты ставит имперсонация, свой UA не отправляем вовсе).
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


def _retry_after_seconds(resp) -> float:
    """Retry-After в секундах: по RFC 7231 заголовок может быть и числом,
    и HTTP-датой. Дата/мусор не должны ронять проход — возвращаем запасные 5 с.
    """
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return 5.0
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 5.0


def _parse_gap(raw: str, default: tuple[float, float]) -> tuple[float, float]:
    """'6,14' / '6-14' / '6' → (6.0, 14.0). Мусор — дефолт."""
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return default
    for sep in (",", "-", "–", ";"):
        if sep in s:
            lo, _, hi = s.partition(sep)
            try:
                return (max(0.0, float(lo)), max(0.0, float(hi)))
            except ValueError:
                return default
    try:
        v = max(0.0, float(s))
        return (v, v)
    except ValueError:
        return default


class HirifyClient:
    """Асинхронный клиент с полем cookie, который можно менять на ходу."""

    def __init__(self, config):
        self.cfg = config
        self.cookie: str = config.hirify_cookie
        self._httpx: Optional[httpx.AsyncClient] = None
        self._curl: Optional["CurlAsyncSession"] = None  # type: ignore[valid-type]
        self.engine: str = "none"   # 'curl_cffi(<target>)' | 'httpx' | 'httpx+h2'
        self._last_request_ts = 0.0
        # «Человеческий» темп: лента — шустро, вакансия — читаем, контакты —
        # не мгновенно. Диапазоны из конфига (env), чтобы тесты зануляли.
        self.gaps: dict[str, tuple[float, float]] = {
            "feed": (config.fetch_delay_sec, config.fetch_delay_sec + 0.4),
            "vacancy": _parse_gap(config.vacancy_gap_sec, (6.0, 14.0)),
            "contacts": _parse_gap(config.contacts_gap_sec, (4.0, 9.0)),
        }

    # ---------- жизненный цикл ----------

    async def open(self) -> None:
        if self._curl is not None or self._httpx is not None:
            return
        target = (getattr(self.cfg, "impersonate_target", "") or "chrome").strip()
        proxy = (getattr(self.cfg, "hirify_proxy", "") or "").strip() or None
        if proxy:
            # креды прокси в лог не светим: scheme://***@host:port
            masked = re.sub(r"//[^@/]+@", "://***@", proxy)
            log.info("Запросы к hirify через прокси %s (стабильный IP вместо датацентра Actions)", masked)
        if HAS_CURL_CFFI:
            try:
                self._curl = CurlAsyncSession(impersonate=target, proxy=proxy)  # type: ignore[misc]
                self.engine = f"curl_cffi({target})"
                log.info("Двигатель запросов: curl_cffi, имперсонация %s (TLS/HTTP2 как Chrome)", target)
            except Exception as e:  # noqa: BLE001 - любая ошибка инициализации
                log.warning("curl_cffi не поднялся (%s) — откат на httpx", e)
                self._curl = None
        if self._curl is None:
            try:
                self._httpx = httpx.AsyncClient(
                    headers=dict(BROWSER_HEADERS),
                    timeout=self.cfg.request_timeout_sec,
                    follow_redirects=True,
                    http2=True,
                    proxy=proxy,
                )
                self.engine = "httpx+h2"
            except ImportError:
                self._httpx = httpx.AsyncClient(
                    headers=dict(BROWSER_HEADERS),
                    timeout=self.cfg.request_timeout_sec,
                    follow_redirects=True,
                    http2=False,
                    proxy=proxy,
                )
                self.engine = "httpx"
            log.info("Двигатель запросов: %s", self.engine)

    async def close(self) -> None:
        if self._curl is not None:
            await self._curl.close()
            self._curl = None
        if self._httpx is not None:
            await self._httpx.aclose()
            self._httpx = None

    def set_cookie(self, cookie: str) -> None:
        """Обновляем cookie на ходу (например, командой /setcookie)."""
        self.cookie = (cookie or "").strip()

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie)

    # ---------- запросы ----------

    async def _throttle(self, kind: str = "feed") -> None:
        """Пауза перед запросом — своя для каждого «жанра» действия.

        feed:     минимум между подряд идущими запросами (как раньше);
        vacancy:  случайная пауза «читаю вакансию» (секунды, не миллисекунды);
        contacts: пауза «дочитал → кликнул Show contacts».

        Ровный интервал в 1.5 с между открытиями вакансий — метроном,
        каким люди не листают джоб-борды.
        """
        lo, hi = self.gaps.get(kind, self.gaps["feed"])
        if kind == "feed":
            elapsed = time.monotonic() - self._last_request_ts
            if elapsed < lo:
                await asyncio.sleep(lo - elapsed + random.uniform(0.0, 0.4))
        elif hi > 0:
            await asyncio.sleep(random.uniform(lo, hi))
        self._last_request_ts = time.monotonic()

    async def _transport(self, method: str, url: str, headers: dict):
        """Одна попытка запроса → (status, text, headers-маппинг).

        Ошибки транспорта (сеть/таймаут/TLS) пробрасываются наверх —
        их ловит ретрай-петля _api_call/_get.
        """
        if self._curl is not None:
            resp = await self._curl.request(
                method, url, headers=headers,
                timeout=self.cfg.request_timeout_sec,
                allow_redirects=True,
            )
            # Гостевые/серверные Set-Cookie не копим: кто мы в каждом запросе,
            # решает только наш явный заголовок Cookie (или его отсутствие).
            try:
                self._curl.cookies.clear()
            except Exception:  # noqa: BLE001 - jar может быть уже пуст
                pass
        else:
            assert self._httpx is not None, "Клиент не открыт — вызовите open()"
            resp = await self._httpx.request(method, url, headers=headers)
            try:
                self._httpx.cookies.jar.clear()
            except Exception:  # noqa: BLE001
                pass
        return resp

    def _headers(self, auth: bool = False) -> dict:
        """Заголовки HTML-страниц сайта (лендинги фидов, страницы вакансий).

        auth=False по умолчанию: публичные страницы читаем анонимно —
        статус аккаунта не должен светиться в каждом проходе по ленте.
        """
        h: dict = {}
        if self.engine.startswith("httpx"):
            h.update(BROWSER_HEADERS)
        else:
            # curl_cffi: UA/sec-ch-ua даёт имперсонация — не дублируем своим
            h.update({
                "Accept": BROWSER_HEADERS["Accept"],
                "Accept-Language": BROWSER_HEADERS["Accept-Language"],
                "Referer": BROWSER_HEADERS["Referer"],
                "Sec-Fetch-Dest": BROWSER_HEADERS["Sec-Fetch-Dest"],
                "Sec-Fetch-Mode": BROWSER_HEADERS["Sec-Fetch-Mode"],
                "Sec-Fetch-Site": BROWSER_HEADERS["Sec-Fetch-Site"],
                "Upgrade-Insecure-Requests": "1",
            })
        if auth and self.cookie:
            h["Cookie"] = self.cookie
        return h

    def _api_headers(self, post: bool = False, auth: bool = True) -> dict:
        """Заголовки для JSON-API api.hirify.me (Laravel Sanctum).

        POST-эндпоинтам нужен X-XSRF-TOKEN — расшифрованное значение cookie
        XSRF-TOKEN (как делает фронтенд сайта), иначе сервер ответит 419.

        auth=True — запрос от имени аккаунта (вакансии, контакты);
        auth=False — анонимный фид: cookie не отправляем вовсе.
        """
        h = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": "https://hirify.me/jobs/",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if auth and self.cookie:
            h["Cookie"] = self.cookie
            if post:
                for part in self.cookie.split(";"):
                    k, _, val = part.strip().partition("=")
                    if k == "XSRF-TOKEN":
                        h["X-XSRF-TOKEN"] = unquote(val)
                        break
        return h

    async def _api_call(self, method: str, path: str, auth: bool = True,
                        kind: str = "vacancy") -> dict:
        """GET/POST к api.hirify.me с общими ретраями и обработкой статусов."""
        await self._throttle(kind)
        url = self.cfg.hirify_api_base + path
        last_status: int | None = None
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._transport(
                    method, url, self._api_headers(post=(method == "POST"), auth=auth))
                last_status = resp.status_code
                if resp.status_code in (404, 410):
                    raise HirifyNotFound(f"hirify вернул HTTP {resp.status_code} для {url}")
                if resp.status_code in (401, 419) and auth:
                    # сессия не работает: cookie истёк/отозван (анонимному фиду
                    # 401 не страшен — он и не должен пускать, но не «ломает» нас)
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
                    retry_after = _retry_after_seconds(resp)
                    log.warning("hirify api: 429, жду %.1fs", retry_after)
                    await asyncio.sleep(min(max(retry_after, 0.0) + 1.0, 60.0))
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
            except TRANSPORT_ERRORS as e:  # network / timeout / TLS
                last_err = e
                await asyncio.sleep(1.5 * (attempt + 1))
        detail = f"HTTP {last_status}" if last_status is not None else last_err
        raise HirifyError(f"Не удалось загрузить {url}: {detail}")

    async def _get(self, url: str, auth: bool = False) -> str:
        await self._throttle("feed")
        last_err: Exception | None = None
        last_status: int | None = None
        for attempt in range(3):
            try:
                resp = await self._transport("GET", url, self._headers(auth=auth))
                last_status = resp.status_code
                if resp.status_code in (404, 410):
                    # вакансия удалена — ретраить бессмысленно
                    raise HirifyNotFound(f"hirify вернул HTTP {resp.status_code} для {url}")
                if resp.status_code == 429:
                    retry_after = _retry_after_seconds(resp)
                    log.warning("hirify: 429, жду %.1fs", retry_after)
                    await asyncio.sleep(min(max(retry_after, 0.0) + 1.0, 60.0))
                    continue
                if resp.status_code in (502, 503, 504):
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                if resp.status_code != 200:
                    raise HirifyError(f"hirify вернул HTTP {resp.status_code} для {url}")
                return resp.text
            except TRANSPORT_ERRORS as e:  # network / timeout / TLS
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
        return await self._api_call("GET", f"/api/vacancies/{slug}",
                                    auth=True, kind="vacancy")

    async def fetch_contacts(self, slug: str) -> dict:
        """Прямые контакты вакансии: POST /api/vacancies/<slug>/contacts.

        Возвращает {contacts: [{type, value, short_code}], access_type, …}.
        Значение value — юзернейм рекрутера или t.me-ссылка (пост в канале).
        """
        return await self._api_call("POST", f"/api/vacancies/{slug}/contacts",
                                    auth=True, kind="contacts")

    async def fetch_feed_api(self, feed, page: int = 1) -> list[dict]:
        """Фид раздела из JSON-API — АНОНИМНО: только вакансии с прямыми
        TG-контактами, серверная фильтрация (HTML-лендинг такое не умеет).

        GET /api/vacancies?{feed.api_params}&{feed_params}&page=N — фильтры
        работают на сервере (в отличие от query лендинга HTML!). Возвращает
        список элементов-«карточек» (id, slug, title, tldr, work_format,
        is_scam, main_stack, …; полное описание — отдельным запросом
        fetch_vacancy). Cookie НЕ отправляем: круглосуточный поллинг фида
        не должен портить статистику аккаунта.
        """
        params = "&".join(
            p for p in self.cfg.feed_params.split("&")
            if p and not p.startswith("page=")
        )
        q = "&".join(x for x in (feed.api_params, params, f"page={max(1, page)}") if x)
        data = await self._api_call("GET", f"/api/vacancies?{q}", auth=False, kind="feed")
        if isinstance(data, dict):
            items = data.get("data") or []
            return items if isinstance(items, list) else []
        return data if isinstance(data, list) else []
