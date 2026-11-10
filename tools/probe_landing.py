"""Пресеты лендингов hirify → параметры API-фида; полнота полей элемента списка.

Запускается на GitHub Actions (probe.yml):
  1) чанки лендингов /web3-crypto-jobs и /remote-jobs: ищем, как SPA задаёт
     фильтр для лендинга (объект «путь → параметры»);
  2) живые тесты GET /api/vacancies с work_format/remote_type/... (cookie);
  3) сравнение полей элемента СПИСКА с детальными — можно ли не делать GET детали;
  4) POST contacts для пары свежих TG-вакансий — бесплатный ли доступ.
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
BASE_FILTER = "only_direct_contact=1&contact_types=telegram&sort_by=newest"


def section(t: str) -> None:
    print(f"\n===== {t} =====")


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
        "Referer": SITE + "/",
        "Cookie": cookie,
    }
    if xsrf:
        hdrs["X-XSRF-TOKEN"] = xsrf

    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                 "Referer": SITE + "/"},
        follow_redirects=True, timeout=40.0,
    ) as c:

        section("1. ЧАНКИ: КАК ЛЕНДИНГ ЗАДАЁТ ФИЛЬТР")
        hits: list[str] = []
        for landing in ("web3-crypto-jobs", "remote-jobs", "remote-it-jobs"):
            try:
                r = await c.get(f"{SITE}/{landing}")
            except Exception as e:  # noqa: BLE001
                print(f"  {landing}: {e}")
                continue
            chunks = sorted(set(re.findall(r'/_nuxt/[A-Za-z0-9._-]+\.js', r.text)))[:30]
            for ch in chunks:
                try:
                    js = (await c.get(SITE + ch)).text
                except Exception:  # noqa: BLE001
                    continue
                for m in re.finditer(r'.{110}' + landing + r'.{170}', js):
                    t = m.group(0).replace("\n", " ")
                    # выкидываем словари переводов (en:"…",ru:"…")
                    if 'en:"' in t or '"en"' in t or "en:" in t[:60]:
                        continue
                    hits.append(t[:290])
        for t in hits[:10]:
            print(f"  …{t}…")

        section("2. ЖИВЫЕ ТЕСТЫ ФИЛЬТРОВ ЛЕНДИНГОВ")
        variants = [
            ("remote?", "work_format=remote"),
            ("global?", "remote_type=global"),
            ("global?2", "work_format=remote&remote_type=global"),
            ("crypto: tags", "tags=blockchain"),
            ("crypto: search", "search=crypto"),
            ("crypto: specialization", "specializations=blockchain"),
        ]
        for label, extra in variants:
            q = f"{BASE_FILTER}&{extra}&page=1"
            try:
                rr = await c.get(f"{API}/api/vacancies?{q}", headers=hdrs)
                if rr.status_code != 200:
                    print(f"  {label:18} -> HTTP {rr.status_code}: {rr.text[:120]!r}")
                    continue
                items = rr.json().get("data") or []
                wfs = {}
                rts = {}
                for it in items:
                    for w in (it.get("work_format") or []):
                        wfs[w] = wfs.get(w, 0) + 1
                    if it.get("remote_type"):
                        rts[it["remote_type"]] = rts.get(it["remote_type"], 0) + 1
                tags = [t.get("name") for it in items[:3] for t in (it.get("tags") or [])][:6]
                print(f"  {label:18} -> {len(items)} шт. | work_format={wfs} | remote_type={rts} | теги={tags}")
            except Exception as e:  # noqa: BLE001
                print(f"  {label} -> ошибка {e}")
            await asyncio.sleep(1.2)

        section("3. ПОЛЯ ЭЛЕМЕНТА СПИСКА vs ДЕТАЛЬ")
        rr = await c.get(f"{API}/api/vacancies?{BASE_FILTER}&page=1", headers=hdrs)
        items = rr.json().get("data") or []
        if items:
            it = items[0]
            print(f"  ключи элемента списка ({len(it)}): {sorted(it)[:28]}")
            print(f"  text: {len(it.get('text') or '')} симв.; tldr: {bool(it.get('tldr'))}; "
                  f"salary: {it.get('salary')!r}")
            slug = it["slug"]
            rd = await c.get(f"{API}/api/vacancies/{slug}", headers=hdrs)
            det = rd.json()
            same = set(it) & set(det)
            only_det = set(det) - set(it)
            only_list = set(it) - set(det)
            print(f"  деталь: text {len(det.get('text') or '')} симв. | общих полей: {len(same)}")
            print(f"  только в детали: {sorted(only_det)[:12]}")
            print(f"  только в списке: {sorted(only_list)[:12]}")
            text_same = (it.get("text") or "") == (det.get("text") or "")
            print(f"  text совпадает: {text_same}")

        section("4. POST contacts ДЛЯ СВЕЖИХ TG-ВАКАНСИЙ")
        for it in items[:2]:
            slug = it["slug"]
            rr = await c.post(f"{API}/api/vacancies/{slug}/contacts", headers=hdrs)
            print(f"  {slug} -> HTTP {rr.status_code}: {rr.text[:200]}")
            await asyncio.sleep(1.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
