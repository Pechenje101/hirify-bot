"""Вторая диагностика hirify.me: API, JS-бандлы, механизм «Show contacts».

Запускается на GitHub Actions (см. .github/workflows/probe.yml):
    python tools/probe_api.py

Шаги:
  1) страница telegram-источниковой вакансии сырым HTML → наш парсер:
     что видно (описание? факты? маркеры скрытия? t.me-ссылки?);
  2) контекст вокруг «Show contacts» — что за кнопка/ссылка/JS;
  3) все JS-бандлы страницы → grep на "/api/…" и auth-механику;
  4) перебор правдоподобных API-эндпоинтов (может, отдаёт данные без cookie?);
  5) RSS/фиды с полным содержимым.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.parsing import parse_job_page  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

JOB = "https://hirify.me/jobs/1342174-telegram-outreach-manager-igaming"
JOB_ID = "1342174"
TME_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/[A-Za-z0-9_+/%-]+", re.I)
MARKERS_RE = re.compile(r"(?i)(show contacts|available after authorization|после авторизации)")


def section(t: str) -> None:
    print(f"\n===== {t} =====")


async def get(c: httpx.AsyncClient, url: str) -> httpx.Response:
    r = await c.get(url)
    print(f"  GET {url} -> {r.status_code} ({len(r.content)} байт, {r.headers.get('content-type', '')[:40]})")
    return r


async def main() -> int:
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"},
        follow_redirects=True, timeout=30.0,
    ) as c:

        section("1. СЫРОЙ HTML TELEGRAM-ИСТОЧНИКОВОЙ ВАКАНСИИ")
        r = await get(c, JOB)
        html = r.text
        v = parse_job_page(html, JOB)
        print(f"  парсер: заголовок={v.title!r}, компания={v.company!r}")
        print(f"  описание: {len(v.description)} симв.; хвост: ...{v.description[-220:]!r}")
        print(f"  contacts_hidden={v.contacts_hidden}, has_direct_contact={v.has_direct_contact}")
        for ct in v.contacts:
            print(f"    • [{ct.kind}] {ct.display} ({ct.source})")
        print(f"  маркеры скрытия: {len(MARKERS_RE.findall(html))}")
        print(f"  t.me-ссылки: {sorted(set(TME_RE.findall(html)))[:10]}")
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        print(f"  JSON-LD: {'есть' if m else 'НЕТ'}")

        section("2. КОНТЕКСТ «SHOW CONTACTS»")
        i = html.lower().find("show contacts")
        if i < 0:
            i = html.lower().find("available after authorization")
        if i >= 0:
            print(f"  ...{html[max(0, i - 400):i + 500]!r}...")
        else:
            print("  маркер не найден в сыром HTML (контент, вероятно, рисует JS)")

        section("3. JS-БАНДЛЫ → API-ПУТИ И AUTH")
        srcs = sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', html)))
        print(f"  скриптов: {len(srcs)}")
        api_paths: set[str] = set()
        auth_hits: list[str] = []
        for src in srcs:
            url = src if src.startswith("http") else f"https://hirify.me{src}"
            try:
                br = await c.get(url)
                js = br.text
            except Exception as e:  # noqa: BLE001
                print(f"  ! {url}: {e}")
                continue
            for m2 in re.finditer(r'["\'](/(?:api|v\d|auth|oauth)[^"\']{2,60})["\']', js):
                api_paths.add(m2.group(1))
            for kw in ("oauth.telegram", "telegram.*widget", "login", "authorization"):
                for m2 in re.finditer(r".{0,60}" + kw + r".{0,60}", js[:2_000_000], re.I):
                    auth_hits.append(m2.group(0).replace("\n", " ")[:130])
        print(f"  API-пути из бандлов: {sorted(api_paths)[:30]}")
        print("  auth-упоминания (первые 12): ")
        for h in auth_hits[:12]:
            print(f"    {h}")

        section("4. ПЕРЕБОР КАНДИДАТОВ API (без cookie)")
        guesses = [
            f"/api/jobs/{JOB_ID}",
            f"/api/jobs/{JOB_ID}/contacts",
            f"/api/v1/jobs/{JOB_ID}",
            f"/api/vacancies/{JOB_ID}",
            f"/api/jobs/{JOB_ID}/contact",
            f"/jobs/{JOB_ID}.json",
            f"/api/jobs?ids={JOB_ID}",
        ]
        for g in guesses:
            try:
                rr = await c.get(f"https://hirify.me{g}")
                body = rr.text[:180].replace("\n", " ")
                print(f"  {g} -> {rr.status_code}: {body!r}")
            except Exception as e:  # noqa: BLE001
                print(f"  {g} -> ошибка {e}")

        section("5. RSS/ПОЛНЫЕ ФИДЫ")
        for g in ("/rss", "/feed.xml", "/jobs.rss", "/rss.xml", "/atom.xml", "/api/feed"):
            try:
                rr = await c.get(f"https://hirify.me{g}")
                if rr.status_code == 200 and len(rr.content) > 200:
                    print(f"  {g} -> {rr.status_code}: {rr.text[:200]!r}")
                else:
                    print(f"  {g} -> {rr.status_code}")
            except Exception as e:  # noqa: BLE001
                print(f"  {g} -> ошибка {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
