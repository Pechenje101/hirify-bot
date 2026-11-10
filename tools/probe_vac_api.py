"""Проверка API вакансий hirify с session-cookie: структура JSON.

Запускается на GitHub Actions (probe.yml). Секрет HIRIFY_COOKIE уже в репо.
Пробуем: /api/vacancies/{slug}, /contacts, /original-text, /similar.
Печатаем ключи JSON и образцы значений (сенситивное маскируется).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SITE = "https://hirify.me"
API = "https://api.hirify.me"
SLUG = "1342174-telegram-outreach-manager-igaming"


def _mask(s, keep=3):
    s = str(s)
    return s if len(s) <= keep * 2 else s[:keep] + "…" + s[-keep:]


def show(label: str, obj, depth: int = 0) -> None:
    pad = "  " * (depth + 1)
    if isinstance(obj, dict):
        print(f"{pad}{label}:")
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and depth < 2:
                show(k, v, depth + 1)
            else:
                vs = json.dumps(v, ensure_ascii=False)[:120] if not isinstance(v, str) else v[:120]
                print(f"{pad}  {k}: {vs!r}")
    elif isinstance(obj, list):
        print(f"{pad}{label}: list[{len(obj)}]")
        for i, item in enumerate(obj[:3]):
            show(f"[{i}]", item, depth + 1)
    else:
        print(f"{pad}{label}: {str(obj)[:120]!r}")


async def main() -> int:
    cookie = os.environ.get("HIRIFY_COOKIE", "")
    xsrf = ""
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "XSRF-TOKEN":
            xsrf = unquote(v)
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Origin": SITE,
        "Referer": f"{SITE}/jobs/{SLUG}",
        "Cookie": cookie,
    }
    if xsrf:
        hdrs["X-XSRF-TOKEN"] = xsrf

    async with httpx.AsyncClient(follow_redirects=True, timeout=40.0) as c:
        for path in (
            f"/api/vacancies/{SLUG}",
            f"/api/vacancies/{SLUG}/contacts",
            f"/api/vacancies/{SLUG}/original-text",
            f"/api/vacancies/{SLUG}/similar",
        ):
            print(f"\n===== GET {path} =====")
            try:
                r = await c.get(API + path, headers=hdrs)
                print(f"  HTTP {r.status_code}, {len(r.content)} байт, {r.headers.get('content-type','')[:30]}")
                if r.status_code == 405:
                    print("  (GET не поддерживается — нужен POST)")
                    continue
                try:
                    data = r.json()
                except Exception:  # noqa: BLE001
                    print(f"  не JSON: {r.text[:200]!r}")
                    continue
                if isinstance(data, dict) and "contact" in json.dumps(list(data.keys())).lower():
                    show("BODY", data)
                else:
                    show("BODY", data, depth=0 if len(json.dumps(data)) < 3000 else 2)
            except Exception as e:  # noqa: BLE001
                print(f"  ошибка: {e}")
            await asyncio.sleep(1.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
