"""Поиск API списка вакансий (фида) и пресетов лендингов + доступ к контактам.

Запускается на GitHub Actions (probe.yml). Шаги:
  1) скачивает чанки страницы фида и ищет:
     а) эндпоинт списка вакансий (как SPA запрашивает фид);
     б) определение пресета лендинга remote-it-jobs (какие фильтры он задаёт);
  2) пробует GET /api/vacancies с разными фильтрами (cookie из секретов);
  3) для первой TG-вакансии из результата — POST contacts: бесплатный доступ?
"""
from __future__ import annotations

import asyncio
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
FEED_PAGE = f"{SITE}/remote-it-jobs"


def section(t: str) -> None:
    print(f"\n===== {t} =====")


async def main() -> int:
    cookie = os.environ.get("HIRIFY_COOKIE", "")
    xsrf = ""
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "XSRF-TOKEN":
            xsrf = unquote(v)

    async with httpx.AsyncClient(
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": SITE + "/",
        },
        follow_redirects=True, timeout=40.0,
    ) as c:

        section("1. ЧАНКИ СТРАНИЦЫ ФИДА → ЭНДПОИНТ СПИСКА И ПРЕСЕТ ЛЕНДИНГА")
        r = await c.get(FEED_PAGE)
        chunks = sorted(set(re.findall(r'/_nuxt/[A-Za-z0-9._-]+\.js', r.text)))
        print(f"  чанков: {len(chunks)}")
        list_hits: set[str] = set()
        preset_ctx: list[str] = []
        for ch in chunks:
            try:
                js = (await c.get(SITE + ch)).text
            except Exception:  # noqa: BLE001
                continue
            for m in re.finditer(r'["\'`]((?:/api/vacancies)[^"\'`]{0,70})["\'`]', js):
                list_hits.add(m.group(1))
            for m in re.finditer(r'.{120}remote-it-jobs.{180}', js):
                preset_ctx.append(m.group(0).replace("\n", " ")[:320])
        print("  пути /api/vacancies* из чанков:")
        for p in sorted(list_hits):
            print(f"    {p}")
        print("  контекст пресета remote-it-jobs:")
        for t in preset_ctx[:6]:
            print(f"    …{t}…")

        section("2. GET /api/vacancies С ФИЛЬТРАМИ (cookie)")
        hdrs = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": SITE + "/remote-it-jobs",
            "Cookie": cookie,
        }
        if xsrf:
            hdrs["X-XSRF-TOKEN"] = xsrf
        queries = [
            "only_direct_contact=1&contact_types=telegram&sort_by=newest&page=1",
            "sources=telegram&sort_by=newest&page=1",
            "only_direct_contact=1&sources=telegram&contact_types=telegram&sort_by=newest&page=1",
            "sort_by=newest&page=1",
        ]
        best_items = None
        for q in queries:
            try:
                rr = await c.get(f"{API}/api/vacancies?{q}", headers=hdrs)
                body = rr.text[:150].replace("\n", " ")
                print(f"  ?{q} -> HTTP {rr.status_code}: {body!r}")
                if rr.status_code == 200:
                    data = rr.json()
                    items = data if isinstance(data, list) else (
                        data.get("data") or data.get("vacancies") or data.get("items") or [])
                    print(f"     ключи: {list(data)[:8] if isinstance(data, dict) else 'list'}; "
                          f"элементов: {len(items)}")
                    for it in items[:3]:
                        if isinstance(it, dict):
                            print(f"       {it.get('slug')} | source={it.get('source')} | "
                                  f"company={it.get('company_title')!r}")
                    if "sources=telegram" in q and items:
                        best_items = items
            except Exception as e:  # noqa: BLE001
                print(f"  ?{q} -> ошибка {e}")
            await asyncio.sleep(1.5)

        section("3. POST contacts ДЛЯ TG-ВАКАНСИИ (доступ бесплатный?)")
        if best_items:
            slug = best_items[0].get("slug")
            if xsrf:
                hdrs["X-XSRF-TOKEN"] = xsrf
            rr = await c.post(f"{API}/api/vacancies/{slug}/contacts", headers=hdrs)
            print(f"  POST /api/vacancies/{slug}/contacts -> HTTP {rr.status_code}")
            print(f"  ответ: {rr.text[:300]}")
        else:
            print("  TG-вакансий в результатах не нашлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
