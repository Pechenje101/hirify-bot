"""MCP-эндпоинт hirify: initialize → tools/list → вызов инструмента поиска.

POST https://api.hirify.me/api/mcp — Laravel Sanctum: нужны Cookie + X-XSRF-TOKEN.
Протокол MCP (streamable HTTP): initialize → initialized → tools/list → tools/call.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SITE = "https://hirify.me"
API = "https://api.hirify.me"
MCP = f"{API}/api/mcp"


def section(t: str) -> None:
    print(f"\n===== {t} =====")


def parse_mcp(text: str):
    """Ответ может быть JSON или SSE-потоком — достаём первый JSON."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("{"):
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            return None
    for line in t.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
    return None


async def main() -> int:
    cookie = os.environ.get("HIRIFY_COOKIE", "")
    xsrf = ""
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "XSRF-TOKEN":
            xsrf = unquote(v)
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Origin": SITE,
        "Referer": SITE + "/mcp-api",
        "Cookie": cookie,
    }
    if xsrf:
        hdrs["X-XSRF-TOKEN"] = xsrf

    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as c:
        section("1. INITIALIZE")
        r = await c.post(MCP, headers=hdrs, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "hirify-bot", "version": "1.0"},
            }})
        print(f"  HTTP {r.status_code}; content-type: {r.headers.get('content-type', '')[:40]}")
        print(f"  заголовки сессии: {[k for k in r.headers if 'session' in k.lower() or 'mcp' in k.lower()]}")
        data = parse_mcp(r.text)
        print(f"  ответ: {json.dumps(data, ensure_ascii=False)[:500] if data else r.text[:300]!r}")
        if r.status_code != 200 or not data or "result" not in (data or {}):
            print("  initialize не прошёл — дальше пробовать бессмысленно")
            return 1

        session_id = r.headers.get("mcp-session-id", "")
        if session_id:
            hdrs["Mcp-Session-Id"] = session_id
            print(f"  mcp-session-id: {session_id[:12]}***")

        section("2. INITIALIZED + TOOLS/LIST")
        try:
            await c.post(MCP, headers=hdrs, json={
                "jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:  # noqa: BLE001
            pass
        r = await c.post(MCP, headers=hdrs, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        data = parse_mcp(r.text)
        tools = ((data or {}).get("result") or {}).get("tools") or []
        print(f"  HTTP {r.status_code}; инструментов: {len(tools)}")
        for t in tools:
            print(f"    • {t.get('name')}: {(t.get('description') or '')[:110]}")
            schema = (t.get("inputSchema") or {}).get("properties") or {}
            if schema:
                print(f"      аргументы: {sorted(schema)[:12]}")

        section("3. ВЫЗОВ ИНСТРУМЕНТА ПОИСКА (если есть)")
        search_names = [t.get("name") for t in tools
                        if any(k in (t.get("name") or "").lower()
                               for k in ("search", "vacanc", "job", "feed"))]
        if not search_names:
            print("  поисковых инструментов не видно")
            return 0
        name = search_names[0]
        schema = next((t.get("inputSchema") or {}).get("properties") or {}
                      for t in tools if t.get("name") == name)
        args = {}
        for key in ("query", "search", "text", "keywords", "q"):
            if key in schema:
                args[key] = "python developer"
                break
        for key in ("only_direct_contact", "direct_contact"):
            if key in schema:
                args[key] = True
        for key in ("contact_types", "contact_type"):
            if key in schema:
                args[key] = ["telegram"]
        for key in ("limit", "page", "per_page"):
            if key in schema:
                args[key] = 3
        print(f"  вызываем {name} с аргументами {args}")
        r = await c.post(MCP, headers=hdrs, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": name, "arguments": args}})
        data = parse_mcp(r.text)
        print(f"  HTTP {r.status_code}")
        blob = json.dumps(data, ensure_ascii=False)
        print(f"  ответ ({len(blob)} симв.): {blob[:1500]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
