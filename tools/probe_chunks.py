"""Поиск API вакансий в чанках Nuxt + проверка его с session-cookie.

Запускается на GitHub Actions (probe.yml). Что делает:
  1) читает страницу вакансии (HTML) и собирает все /_nuxt/*.js чанки;
  2) скачивает их и ищет пути вида /api/vacancies/... и логику show_contacts;
  3) с секретом HIRIFY_COOKIE дёргает найденные эндпоинты и печатает
     структуру ответа (ключи JSON, без утечки cookie).
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
JOB = f"{SITE}/jobs/1342174-telegram-outreach-manager-igaming"
JOB_ID = "1342174"


def section(t: str) -> None:
    print(f"\n===== {t} =====")


async def main() -> int:
    cookie = os.environ.get("HIRIFY_COOKIE", "")
    xsrf = ""
    if cookie:
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

        section("1. СТРАНИЦА ВАКАНСИИ + СПИСОК ЧАНКОВ")
        r = await c.get(JOB)
        html = r.text
        chunks = sorted(set(re.findall(r'/_nuxt/[A-Za-z0-9._-]+\.js', html)))
        print(f"  чанков: {len(chunks)}; cookie в этом ране: {'есть' if cookie else 'нет'}")

        section("2. СКАЧИВАЕМ ЧАНКИ, ИЩЕМ ПУТИ API")
        api_paths: set[str] = set()
        contact_ctx: list[str] = []
        for ch in chunks:
            url = SITE + ch
            try:
                br = await c.get(url)
                js = br.text
            except Exception as e:  # noqa: BLE001
                print(f"  ! {url}: {e}")
                continue
            for m in re.finditer(r'["\'`]((?:/api/|/auth/)[A-Za-z0-9_${}/.:-]{2,60})["\'`]', js):
                api_paths.add(m.group(1))
            for m in re.finditer(r'.{80}(?:show_?contacts|contactsVisible|contact_breadth).{120}', js):
                t = m.group(0).replace("\n", " ")
                if any(k in t for k in ("fetch", "/api", "get", "post", "vacanc")):
                    contact_ctx.append(t[:200])
        print("  все пути из чанков:")
        for p in sorted(api_paths):
            print(f"    {p}")
        print("  контекст show_contacts:")
        for t in contact_ctx[:8]:
            print(f"    …{t}…")

        # шаблоны с подстановкой id: "/api/vacancies/"+e или ${id}
        section("3. ШАБЛОНЫ ПУТЁЙ С id ВАКАНСИИ")
        for ch in chunks[:25]:
            try:
                js = (await c.get(SITE + ch)).text
            except Exception:  # noqa: BLE001
                continue
            for m in re.finditer(r'.{100}["\'`](/api/vacanc[^"\'`]*)["\'`].{100}', js):
                t = m.group(0).replace("\n", " ")
                print(f"  {ch}: …{t[:280]}…")

        if cookie:
            section("4. ПРОВЕРКА НАЙДЕННЫХ ЭНДПОИНТОВ С COOKIE")
            hdrs = {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Origin": SITE,
                "Referer": SITE + "/jobs/",
                "Cookie": cookie,
            }
            if xsrf:
                hdrs["X-XSRF-TOKEN"] = xsrf
            guesses = sorted(p for p in api_paths if "vacanc" in p and "{" not in p)
            for p in guesses[:12]:
                for variant in (p, p.replace("vacancies", f"vacancies/{JOB_ID}")):
                    url = API + variant if variant.startswith("/") else variant
                    try:
                        rr = await c.get(url, headers=hdrs)
                        body = rr.text[:400].replace("\n", " ")
                        print(f"  GET {variant} -> {rr.status_code}: {body!r}")
                    except Exception as e:  # noqa: BLE001
                        print(f"  GET {variant} -> ошибка {e}")
        else:
            print("\n(HIRIFY_COOKIE не задан — только поиск путей, без проверки)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
