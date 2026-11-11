"""Анонимный зонд api.hirify.me — БЕЗ cookie, БЕЗ запросов к вакансиям/контактам.

Отвечает на вопросы, нужные для анти-флаг фиксов:
  1) отдаётся ли фид (GET /api/vacancies) без сессии и сколько в нём элементов;
  2) какие поля у элемента фида (title/tldr/work_format/…) — хватит ли их
     для префильтра, чтобы НЕ открывать каждую вакансию;
  3) чем отвечает сервер: HTTP-версия (h2?), заголовки rate-limit;
  4) покрывает ли search= целевые индустрии (igaming/betting/dating/…),
     чтобы фильтровать мусор ещё на стороне сервера;
  5) ставится ли curl_cffi на раннере и проходит ли имперсонация Chrome
     (TLS/JA3 + HTTP/2) — заготовка под переход с httpx.

Запускать только с GitHub Actions (песочница разработки сайта не видит).
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

API = "https://api.hirify.me"
COMMON = "only_direct_contact=1&contact_types=telegram&sort_by=newest&page=1"
FEEDS = {
    "remote": "work_format=remote",
    "global": "remote_type=global",
    "crypto": "search=crypto",
}
SEARCH_TERMS = ["igaming", "gambling", "betting", "casino", "dating", "web3",
                "blockchain", "high risk", "nda"]

JSON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://hirify.me/jobs/",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


def items_of(data) -> list:
    if isinstance(data, dict):
        x = data.get("data")
        return x if isinstance(x, list) else []
    return data if isinstance(data, list) else []


def show_headers(resp) -> str:
    keep = ("server", "via", "cf-ray", "x-ratelimit-limit", "x-ratelimit-remaining",
            "x-ratelimit-reset", "retry-after", "x-frame-options", "set-cookie")
    parts = [f"{k}={resp.headers[k]}" for k in keep if k in resp.headers]
    hv = getattr(resp, "http_version", "?")
    return f"http={hv} " + " ".join(parts)


async def probe_httpx() -> None:
    print("=" * 72)
    print("ЧАСТЬ 1. httpx (как сейчас работает бот): фид БЕЗ cookie")
    print("=" * 72)
    async with httpx.AsyncClient(
        headers=dict(JSON_HEADERS), timeout=30.0, follow_redirects=True, http2=True,
    ) as c:
        for key, params in FEEDS.items():
            url = f"{API}/api/vacancies?{params}&{COMMON}"
            try:
                r = await c.get(url)
                items = items_of(r.json()) if r.status_code == 200 else []
                print(f"\n[{key}] HTTP {r.status_code}, элементов: {len(items)}")
                print(f"  {show_headers(r)}")
                if items:
                    it = items[0]
                    print(f"  поля элемента: {sorted(it.keys())}")
                    for it in items[:2]:
                        print(f"   · {str(it.get('title'))[:70]!r} "
                              f"tldr={str(it.get('tldr'))[:60]!r} "
                              f"wf={it.get('work_format')} "
                              f"src={it.get('source')}")
            except Exception as e:  # noqa: BLE001
                print(f"[{key}] ОШИБКА: {type(e).__name__}: {e}")
            await asyncio.sleep(1.5)


async def probe_search() -> None:
    print("\n" + "=" * 72)
    print("ЧАСТЬ 2. search= по целевым индустриям (анонимно, страница 1)")
    print("=" * 72)
    async with httpx.AsyncClient(
        headers=dict(JSON_HEADERS), timeout=30.0, follow_redirects=True,
    ) as c:
        for term in SEARCH_TERMS:
            url = f"{API}/api/vacancies?search={term.replace(' ', '+')}&{COMMON}"
            try:
                r = await c.get(url)
                items = items_of(r.json()) if r.status_code == 200 else []
                sample = ""
                if items:
                    sample = " | " + " ; ".join(
                        str(x.get("title"))[:40] for x in items[:2])
                print(f"search={term:<12} HTTP {r.status_code}: {len(items)} шт.{sample}")
            except Exception as e:  # noqa: BLE001
                print(f"search={term:<12} ОШИБКА: {type(e).__name__}: {e}")
            await asyncio.sleep(1.5)


def probe_curl_cffi() -> None:
    print("\n" + "=" * 72)
    print("ЧАСТЬ 3. curl_cffi: имперсонация Chrome (TLS+HTTP/2), анонимный фид")
    print("=" * 72)
    try:
        from curl_cffi import requests as creq
    except ImportError as e:
        print(f"curl_cffi НЕ установлен: {e}")
        return
    import curl_cffi
    print(f"curl_cffi {curl_cffi.__version__}: импорт ок")
    # пробуем свежие цели имперсонации по очереди
    for target in ("chrome", "chrome131", "chrome124", "safari17_0"):
        try:
            with creq.Session(impersonate=target, timeout=30.0) as s:
                r = s.get(f"{API}/api/vacancies?work_format=remote&{COMMON}",
                          headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                                   "Referer": "https://hirify.me/jobs/"})
                n = len(items_of(r.json())) if r.status_code == 200 else -1
                hv = getattr(r, "http_version", None) or (
                    "h2" if r.http_version else "?")
                ua = r.request.headers.get("User-Agent", "?")[:60]
                print(f"impersonate={target}: HTTP {r.status_code}, элементов {n}, "
                      f"http={hv}")
                print(f"  UA от имперсонации: {ua}")
                if r.status_code == 200:
                    print(f"  → РАБОТАЕТ: цель {target} пригодна для боевого клиента")
                    return
        except Exception as e:  # noqa: BLE001
            print(f"impersonate={target}: ОШИБКА {type(e).__name__}: {e}")


async def main() -> int:
    print(f"зонд запущен, python {sys.version.split()[0]}")
    await probe_httpx()
    await probe_search()
    probe_curl_cffi()
    print("\nГОТОВО")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
