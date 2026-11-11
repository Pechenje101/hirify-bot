"""Зонд прокси-кандидатов — анонимная проверка «прокси → hirify.me» из Actions.

Кандидаты берутся из env PROBE_CANDIDATES (запятая-разделённый список,
задан прямо в workflow — это НЕ секреты, просто публичные адреса). Для
каждого: Cloudflare trace (исходящий IP, маскируется) + анонимный фид
api.hirify.me через curl_cffi (имперсонация Chrome), при успехе — ещё httpx.
В финале печатает BEST=<proxy> — лучший по «фид 200» и минимальному времени.

БЕЗ cookie, БЕЗ запросов к вакансиям/контактам — риска для аккаунта ноль.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

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


def check(proxy: str) -> dict:
    out = {"proxy": proxy, "trace": None, "feed": None, "feed_hx": None, "sec": None}
    try:
        from curl_cffi import requests as creq
        t0 = time.monotonic()
        r = creq.get("https://www.cloudflare.com/cdn-cgi/trace",
                     proxy=proxy, timeout=12, impersonate="chrome")
        dt = time.monotonic() - t0
        m = re.search(r"^ip=(.+)$", r.text, re.M)
        loc = re.search(r"^loc=(.+)$", r.text, re.M)
        ip = m.group(1).strip() if m else "?"
        net = ".".join(ip.split(".")[:3]) + ".x" if "." in ip else ":".join(ip.split(":")[:4]) + "::x"
        out["trace"] = f"HTTP {r.status_code} ip={net} loc={loc.group(1) if loc else '?'} {dt:.1f}s"
    except Exception as e:  # noqa: BLE001
        out["trace"] = f"ERR {type(e).__name__}"
        return out

    t0 = time.monotonic()
    try:
        from curl_cffi import requests as creq
        r = creq.get(FEED, proxy=proxy, timeout=20, impersonate="chrome", headers=HEADERS)
        n = len(items_of(json.loads(r.text))) if r.status_code == 200 else -1
        out["feed"] = f"HTTP {r.status_code} n={n}"
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        out["feed"] = f"ERR {type(e).__name__}: {msg[:80]}"
        return out
    try:
        import httpx
        with httpx.Client(proxy=proxy, timeout=20.0, follow_redirects=True,
                          http2=True, headers=HEADERS) as c:
            rr = c.get(FEED)
        out["feed_hx"] = f"HTTP {rr.status_code}"
    except Exception as e:  # noqa: BLE001
        out["feed_hx"] = f"ERR {type(e).__name__}"
    out["sec"] = round(time.monotonic() - t0, 1)
    return out


def main() -> int:
    raw = (os.getenv("PROBE_CANDIDATES") or "").strip()
    if not raw:
        p = (os.getenv("HIRIFY_PROXY") or "").strip()
        if not p:
            print("!! нет ни PROBE_CANDIDATES, ни HIRIFY_PROXY")
            return 1
        raw = p
    cands = [c.strip() for c in raw.split(",") if c.strip()]
    print(f"Кандидатов: {len(cands)} (параллельно, по 5)")

    with ThreadPoolExecutor(max_workers=5) as pool:
        res = list(pool.map(check, cands))

    print("\n=== РЕЗУЛЬТАТЫ ===")
    good = []
    for r in res:
        line = f"{mask(r['proxy']):<34} trace: {r['trace']} | feed: {r['feed']}"
        if r.get("feed_hx"):
            line += f" | hx: {r['feed_hx']}"
        if r.get("sec") is not None:
            line += f" [{r['sec']}s]"
        print(line)
        if r["feed"] and r["feed"].startswith("HTTP 200") and \
           r.get("feed_hx") and r["feed_hx"].startswith("HTTP 200"):
            good.append(r)

    if good:
        best = sorted(good, key=lambda x: x["sec"] or 999)[0]
        print(f"\nBEST={best['proxy']}")
        print("ВЕРДИКТ: есть годной прокси — поставь его в секрет HIRIFY_PROXY")
        return 0
    print("\nВЕРДИКТ: годных нет — нужны новые кандидаты")
    return 1


if __name__ == "__main__":
    sys.exit(main())
