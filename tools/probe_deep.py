"""Третья диагностика hirify.me: скачать реальные HTML/JS как артефакты + API auth.

Запускается на GitHub Actions (probe.yml), результаты — в артефакте probe-files:
  * job_*.html  — сырые страницы вакансий (анонимно);
  * feed_*.html — страница фида;
  * *.js        — JS-бандлы сайта (там эндпоинты и схемы запросов);
  * api_*.json  — ответы API (facets, auth/user).
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
OUT = Path("debug")  # артефакт: весь каталог


def section(t: str) -> None:
    print(f"\n===== {t} =====")


async def main() -> int:
    OUT.mkdir(exist_ok=True)
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"},
        follow_redirects=True, timeout=40.0,
    ) as c:

        jobs = [
            ("1342174-telegram-outreach-manager-igaming", "tg-источник"),
            ("1394942-technical-sound-designer-game-audio", "из фида"),
        ]
        script_srcs: set[str] = set()
        for slug, kind in jobs:
            section(f"HTML вакансии ({kind}): {slug}")
            r = await c.get(f"https://hirify.me/jobs/{slug}")
            (OUT / f"job_{slug}.html").write_text(r.text, encoding="utf-8")
            print(f"  {r.status_code}, {len(r.content)} байт -> debug/job_{slug}.html")
            script_srcs |= set(re.findall(r'<script[^>]+src="([^"]+)"', r.text))

        section("HTML фида")
        r = await c.get(
            "https://hirify.me/remote-it-jobs?only_direct_contact=1"
            "&contact_types=telegram&sort_by=newest&page=1"
        )
        (OUT / "feed_remote_p1.html").write_text(r.text, encoding="utf-8")
        print(f"  {r.status_code}, {len(r.content)} байт -> debug/feed_remote_p1.html")

        section("JS-бандлы сайта")
        for src in sorted(script_srcs):
            url = src if src.startswith("http") else f"https://hirify.me{src}"
            name = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("/")[-1])[:60]
            try:
                br = await c.get(url)
                (OUT / f"js_{name}").write_text(br.text, encoding="utf-8")
                print(f"  {url} -> {br.status_code}, {len(br.content)} байт")
            except Exception as e:  # noqa: BLE001
                print(f"  {url} -> ошибка {e}")

        section("API: vacancies/facets и auth/user (анонимно)")
        for path in ("/api/vacancies/facets", "/auth/user", "/api/config/"):
            try:
                rr = await c.get(f"https://hirify.me{path}")
                body = rr.text[:400].replace("\n", " ")
                (OUT / ("api_" + path.strip("/").replace("/", "_") + ".txt")).write_text(rr.text, encoding="utf-8")
                print(f"  GET {path} -> {rr.status_code}: {body!r}")
            except Exception as e:  # noqa: BLE001
                print(f"  GET {path} -> ошибка {e}")

        section("JS: контекст вокруг 'contact' (эндпоинт показа контактов)")
        blobs = sorted(OUT.glob("js_*"))
        shown = 0
        for f in blobs:
            js = f.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r".{0,70}[Cc]ontacts?.{0,90}", js):
                frag = m.group(0).replace("\n", " ")
                if any(k in frag.lower() for k in ("/api", "auth", "show", "vacanc", "hiring", "hidden")):
                    print(f"  {f.name}: …{frag}…")
                    shown += 1
                    if shown > 40:
                        break
            if shown > 40:
                break

        section("JS: контекст вокруг 'register' (схема регистрации)")
        shown = 0
        for f in blobs:
            js = f.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r".{0,60}register.{0,110}", js):
                frag = m.group(0).replace("\n", " ")
                if any(k in frag.lower() for k in ("email", "otp", "password", "post", "start", "verify")):
                    print(f"  {f.name}: …{frag}…")
                    shown += 1
                    if shown > 30:
                        break
            if shown > 30:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
