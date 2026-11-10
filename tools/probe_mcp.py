"""Разведка официального API/MCP hirify: эндпоинт, токен, протокол.

Запускается на GitHub Actions (probe.yml). Шаги:
  1) страница /mcp-api (публичная) и /account/mcp-api (с cookie): собираем
     JS-чанки маршрута, ищем адрес MCP-эндпоинта, схемы токенов/авторизации;
  2) GET /auth/user с cookie: поле agent_connected и прочее состояние аккаунта;
  3) проба MCP-протокола: POST initialize (JSON-RPC) на найденный эндпоинт.
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
        headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                 "Referer": SITE + "/"},
        follow_redirects=True, timeout=40.0,
    ) as c:

        found_urls: set[str] = set()
        for path, with_cookie in (("/mcp-api", False), ("/account/mcp-api", True)):
            section(f"1. СТРАНИЦА {path}")
            hdrs = {"Cookie": cookie} if with_cookie else {}
            r = await c.get(SITE + path, headers=hdrs)
            print(f"  HTTP {r.status_code}, {len(r.content)} байт")
            chunks = sorted(set(re.findall(r'/_nuxt/[A-Za-z0-9._-]+\.js', r.text)))
            print(f"  чанков: {len(chunks)}")
            for ch in chunks[:40]:
                try:
                    js = (await c.get(SITE + ch)).text
                except Exception:  # noqa: BLE001
                    continue
                if "mcp" not in js.lower() and "agent" not in js.lower():
                    continue
                for m in re.finditer(r'https?://[A-Za-z0-9._-]+/[A-Za-z0-9._/-]{0,40}', js):
                    u = m.group(0)
                    if "hirify" in u or "mcp" in u.lower():
                        found_urls.add(u)
                for m in re.finditer(r'["\'](/(?:api|account|mcp)[A-Za-z0-9_./-]{2,50})["\']', js):
                    found_urls.add(m.group(1))
                for m in re.finditer(r'.{80}(?:api[_-]?key|apiKey|api[_-]?token|bearer|Bearer).{100}', js):
                    frag = m.group(0).replace("\n", " ")
                    if any(k in frag.lower() for k in ("mcp", "agent", "token", "authoriz")):
                        print(f"  AUTH-CTX [{ch.split('/')[-1]}]: …{frag[:200]}…")
            print(f"  найдено URL: {sorted(found_urls)[:15]}")

        section("2. GET /auth/user (состояние аккаунта)")
        hdrs = {
            "User-Agent": UA, "Accept": "application/json, text/plain, */*",
            "Referer": SITE + "/", "Origin": SITE, "Cookie": cookie,
        }
        if xsrf:
            hdrs["X-XSRF-TOKEN"] = xsrf
        r = await c.get(f"{API}/auth/user", headers=hdrs)
        print(f"  HTTP {r.status_code}")
        if r.status_code == 200:
            u = r.json()
            safe = {k: (v if k in (
                "agent_connected", "has_subscription", "is_premium", "user_type_id",
                "available_matches", "available_global", "plan_since",
            ) else "…") for k, v in list(u.items())[:30]}
            print(f"  поля: {json.dumps(safe, ensure_ascii=False)[:600]}")

        section("3. ПРОБА MCP (JSON-RPC initialize)")
        candidates = [u for u in found_urls if "mcp" in u.lower()]
        if not candidates:
            candidates = [f"{SITE}/mcp-api", f"{API}/mcp"]
        for url in candidates[:4]:
            try:
                r = await c.post(url, headers={
                    "User-Agent": UA, "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json", "Referer": SITE + "/mcp-api",
                    "Origin": SITE, "Cookie": cookie,
                }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "hirify-bot", "version": "1.0"},
                }})
                print(f"  POST {url} -> HTTP {r.status_code}, "
                      f"content-type={r.headers.get('content-type', '')[:40]}")
                print(f"  ответ: {r.text[:400]!r}")
            except Exception as e:  # noqa: BLE001
                print(f"  POST {url} -> ошибка {e}")
            await asyncio.sleep(1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
