"""Обновление HIRIFY_COOKIE: повторный вход по сохранённым кредам (email+пароль).

Запускается на GitHub Actions (workflows/refresh.yml — вручную и ежемесячно):
    python tools/refresh_cookie.py

Секреты:
  HIRIFY_EMAIL, HIRIFY_PASSWORD — аккаунт (создан tools/register_hirify.py);
  PAT_TOKEN — чтобы записать новую сессию в секрет HIRIFY_COOKIE.

Схема (Laravel Sanctum, api.hirify.me):
  1) GET  /sanctum/csrf-cookie → XSRF-TOKEN;
  2) POST /auth/login {email, password} (+X-XSRF-TOKEN, Origin) → сессия;
  3) быстрая проверка: POST /api/vacancies/{slug}/contacts с новой cookie;
  4) новая cookie — в секрет репозитория (в логи не попадает).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from nacl import encoding, public  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SITE = "https://hirify.me"
API = "https://api.hirify.me"
TEST_SLUG = "1342174-telegram-outreach-manager-igaming"


def section(t: str) -> None:
    print(f"\n===== {t} =====")


def set_gh_secret(name: str, value: str) -> None:
    token = os.environ["PAT_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    pk = json.loads(urllib.request.urlopen(req).read())
    key = public.PublicKey(pk["key"], encoding.Base64Encoder())
    sealed = public.SealedBox(key).encrypt(value.encode())
    body = json.dumps({
        "encrypted_value": base64.b64encode(sealed).decode(),
        "key_id": pk["key_id"],
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
        data=body, method="PUT",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        print(f"  секрет {name}: HTTP {r.status}")


async def main() -> int:
    email = os.environ.get("HIRIFY_EMAIL", "")
    password = os.environ.get("HIRIFY_PASSWORD", "")
    if not email or not password:
        raise SystemExit("Нужны секреты HIRIFY_EMAIL/HIRIFY_PASSWORD (аккаунт из auth-воркфлоу)")

    async with httpx.AsyncClient(
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Origin": SITE,
            "Referer": SITE + "/",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        },
        follow_redirects=True, timeout=40.0,
    ) as c:

        section("1. CSRF-COOKIE")
        r = await c.get(f"{API}/sanctum/csrf-cookie")
        xsrf = c.cookies.get("XSRF-TOKEN", "")
        print(f"  HTTP {r.status_code}; XSRF-TOKEN: {'есть' if xsrf else 'НЕТ'}")
        auth_headers = {"X-XSRF-TOKEN": unquote(xsrf)} if xsrf else {}

        section("2. POST /auth/login")
        last_err = ""
        for body in ({"email": email, "password": password, "remember": True},
                     {"email": email, "password": password}):
            r = await c.post(f"{API}/auth/login", json=body, headers=auth_headers)
            print(f"  body-вариант {sorted(body)} -> HTTP {r.status_code}")
            if r.status_code in (200, 201):
                break
            last_err = r.text[:300]
        else:
            print(f"  не удалось войти: {last_err}")
            raise RuntimeError("login не прошёл — проверьте HIRIFY_EMAIL/HIRIFY_PASSWORD")

        cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
        print(f"  cookie: {len(cookie)} символов; ключи: {sorted(c.cookies.keys())}")

        section("3. ПРОВЕРКА НОВОЙ СЕССИИ")
        xsrf2 = unquote(c.cookies.get("XSRF-TOKEN", ""))
        r = await c.post(
            f"{API}/api/vacancies/{TEST_SLUG}/contacts",
            headers={"X-XSRF-TOKEN": xsrf2} if xsrf2 else {},
        )
        print(f"  POST contacts -> HTTP {r.status_code}")
        ok = r.status_code == 200
        if ok:
            got = (r.json() or {}).get("contacts") or []
            print(f"  контактов получено: {len(got)}")
        print(f"VERDICT: {'OK — сессия работает' if ok else 'НЕ РАБОТАЕТ'}")

        section("4. СЕКРЕТ РЕПОЗИТОРИЯ")
        set_gh_secret("HIRIFY_COOKIE", cookie)
        print("\nГОТОВО: HIRIFY_COOKIE обновлён.")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
