"""Зонд прокси HIRIFY_PROXY — анонимная проверка «прокси → hirify.me».

Проверяет, что прокси из секрета HIRIFY_PROXY реально годится для бота:
  1) Cloudflare trace через прокси — какой исходящий IP (маскируется);
  2) анонимный фид api.hirify.me через прокси (curl_cffi, имперсонация Chrome);
  3) тот же фид через httpx (fallback-двигатель бота).

БЕЗ cookie, БЕЗ запросов к вакансиям/контактам — риска для аккаунта ноль.
Запуск: workflow_dispatch из .github/workflows/probe-proxy.yml.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

API = "https://api.hirify.me"
FEED = f"{API}/api/vacancies?work_format=remote&only_direct_contact=1&contact_types=telegram&sort_by=newest&page=1"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://hirify.me/jobs/",
}


def mask(s: str) -> str:
    return re.sub(r"//[^@/]+@", "://***@", s)


def items_of(data) -> list:
    if isinstance(data, dict):
        x = data.get("data")
        return x if isinstance(x, list) else []
    return data if isinstance(data, list) else []


def main() -> int:
    proxy = (os.getenv("HIRIFY_PROXY") or "").strip()
    if not proxy:
        print("!! HIRIFY_PROXY пуст — секрет не задан, проверять нечего")
        return 1
    print(f"Прокси: {mask(proxy)}")

    ok_cf = False
    try:
        from curl_cffi import requests as creq
        r = creq.get("https://www.cloudflare.com/cdn-cgi/trace",
                     proxy=proxy, timeout=15, impersonate="chrome")
        m = re.search(r"^ip=(.+)$", r.text, re.M)
        loc = re.search(r"^loc=(.+)$", r.text, re.M)
        ip = (m.group(1).strip() if m else "?")
        # IP целиком не печатаем — только подсеть /24 (v4) или /64 (v6)
        net = ".".join(ip.split(".")[:3]) + ".x" if "." in ip else ":".join(ip.split(":")[:4]) + "::x"
        print(f"1) Cloudflare trace через прокси: HTTP {r.status_code}, "
              f"IP {net}, страна {loc.group(1) if loc else '?'}")
        ok_cf = r.status_code == 200
    except Exception as e:  # noqa: BLE001
        print(f"1) Cloudflare trace через прокси: ОШИБКА {type(e).__name__}: {e}")

    results = {}
    for engine in ("curl_cffi", "httpx"):
        t0 = time.monotonic()
        try:
            if engine == "curl_cffi":
                from curl_cffi import requests as creq
                r = creq.get(FEED, proxy=proxy, timeout=30, impersonate="chrome",
                             headers=HEADERS)
                status, body, ver = r.status_code, r.text, getattr(r, "http_version", "?")
            else:
                import httpx
                with httpx.Client(proxy=proxy, timeout=30.0, follow_redirects=True,
                                  http2=True, headers=HEADERS) as c:
                    rr = c.get(FEED)
                status, body, ver = rr.status_code, rr.text, getattr(rr, "http_version", "?")
            dt = time.monotonic() - t0
            n = len(items_of(json.loads(body))) if status == 200 else -1
            results[engine] = status
            print(f"2) фид через {engine}: HTTP {status}, элементов {n}, "
                  f"время {dt:.1f}s, http={ver}")
        except Exception as e:  # noqa: BLE001
            results[engine] = f"ERR:{type(e).__name__}"
            print(f"2) фид через {engine}: ОШИБКА {type(e).__name__}: {e}")

    verdict = ok_cf and results.get("curl_cffi") == 200
    print(f"\nВЕРДИКТ: {'ПРОКСИ ГОДЕН для HIRIFY_PROXY' if verdict else 'ПРОКСИ НЕ ГОДЕН'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
