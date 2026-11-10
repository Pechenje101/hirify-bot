"""Регистрация аккаунта hirify.me (email-OTP) и запись session-cookie в секреты репо.

Запускается на GitHub Actions (воркфлоу auth.yml): из песочницы разработки сайт
недоступен, а раннику Actions сеть открыта. Cookie и пароль НИКОГДА не попадают
в логи/артефакты: из скрипта они уходят напрямую в секреты репозитория
(HIRIFY_COOKIE / HIRIFY_EMAIL / HIRIFY_PASSWORD) через GitHub API (sealed box).

Схема (из runtime-конфига и JS-бандла сайта):
  * apiBase = https://api.hirify.me — отдельный API-домен (Laravel Sanctum);
  * временный ящик mail.tm (POST /accounts с IP раннеров блокируется — ящик
    создаётся заранее и передаётся секретами REG_EMAIL / REG_MAILTOKEN);
  * 1) GET  {api}/sanctum/csrf-cookie → cookie XSRF-TOKEN (+ сессия);
    2) POST {api}/auth/register/start {name, email, password,
       password_confirmation, locale, auth_source} с X-XSRF-TOKEN и Origin;
    3) письмо с 6-значным OTP на ящик;
    4) POST {api}/auth/register/verify {registration_token, otp} →
       пользователь + Set-Cookie сессии (домен .hirify.me);
  * проверка: страница вакансии на hirify.me с этой cookie — наш парсер
    видит контакт рекрутера? фид с фильтром выдаёт вакансии с контактами?;
  * секреты в репозиторий; на экране — только вердикты и маски.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import string
import sys
import urllib.request
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from nacl import encoding, public  # noqa: E402

from app.parsing import extract_job_links, parse_job_page  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SITE = "https://hirify.me"           # Nuxt-сайт (страницы вакансий)
API = "https://api.hirify.me"        # Laravel API (auth)
JOB_TG_SOURCE = f"{SITE}/jobs/1342174-telegram-outreach-manager-igaming"
FEED_URL = (f"{SITE}/remote-it-jobs?only_direct_contact=1"
            "&contact_types=telegram&sort_by=newest&page=1")

OTP_TIMEOUT_SEC = 240
OTP_POLL_SEC = 6.0
ACCOUNT_NAME = "Alex"          # нейтральное имя профиля
ALPHABET = string.ascii_letters + string.digits


def section(t: str) -> None:
    print(f"\n===== {t} =====")


def _rand(n: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


# --------------------------------------------------------------------- mail.tm

class TempMail:
    """Чтение писем временного ящика mail.tm.

    Ящик либо уже создан снаружи (секреты REG_EMAIL/REG_MAILTOKEN), либо
    создаём сами с ретраями (IP раннеров mail.tm часто блокирует).
    """

    def __init__(self) -> None:
        self.address = os.environ.get("REG_EMAIL", "")
        self.token = os.environ.get("REG_MAILTOKEN", "")
        self.password = ""
        self.account_id = ""
        self._created_here = False

    async def ensure(self) -> None:
        if self.address and self.token:
            print(f"  ящик из секретов: {self.address[:9]}*** — готов")
            return
        print("  секретов REG_EMAIL/REG_MAILTOKEN нет — создаю ящик сам (mail.tm)")
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get("https://api.mail.tm/domains")
            r.raise_for_status()
            domain = r.json()["hydra:member"][0]["domain"]
            for attempt in range(4):
                self.address = f"hirify{_rand(6).lower()}@{domain}"
                self.password = _rand(16)
                r = await c.post("https://api.mail.tm/accounts",
                                json={"address": self.address, "password": self.password})
                if r.status_code == 201:
                    self.account_id = r.json()["id"]
                    r = await c.post("https://api.mail.tm/token",
                                     json={"address": self.address, "password": self.password})
                    r.raise_for_status()
                    self.token = r.json()["token"]
                    self._created_here = True
                    print(f"  ящик создан: {self.address[:7]}***@{domain} (попытка {attempt + 1})")
                    return
                print(f"  попытка {attempt + 1}: HTTP {r.status_code}: {r.text[:200]}")
                await asyncio.sleep(3)
        raise RuntimeError("mail.tm не даёт создать ящик с этого IP "
                           "(заполните секреты REG_EMAIL/REG_MAILTOKEN)")

    async def wait_otp(self) -> str:
        print("  жду письмо с OTP…")
        async with httpx.AsyncClient(timeout=20.0) as c:
            hdr = {"Authorization": f"Bearer {self.token}"}
            for i in range(int(OTP_TIMEOUT_SEC / OTP_POLL_SEC)):
                await asyncio.sleep(OTP_POLL_SEC)
                r = await c.get("https://api.mail.tm/messages", headers=hdr)
                r.raise_for_status()
                items = r.json().get("hydra:member", [])
                for m in items:
                    frm = json.dumps(m.get("from", {}))
                    if "hirify" not in frm.lower() and "code" not in m.get("subject", "").lower():
                        continue
                    r2 = await c.get(f"https://api.mail.tm/messages/{m['id']}", headers=hdr)
                    body = r2.json().get("text") or r2.json().get("html") or ""
                    code = re.search(r"\b(\d{6})\b", body or "")
                    if code:
                        print(f"  OTP получен (на {(i + 1) * OTP_POLL_SEC:.0f}-й секунде)")
                        return code.group(1)
                print(f"  …писем ещё нет ({(i + 1) * OTP_POLL_SEC:.0f} c)")
        raise RuntimeError("письмо с OTP не пришло")

    async def delete(self) -> None:
        if not self._created_here:
            return  # ящик создан снаружи — удаляет владелец секретов
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                await c.delete(f"https://api.mail.tm/accounts/{self.account_id}",
                               headers={"Authorization": f"Bearer {self.token}"})
            print("  временный ящик удалён")
        except Exception as e:  # noqa: BLE001
            print(f"  ящик не удалён (не критично): {e}")


# --------------------------------------------------------------------- секреты

def set_gh_secret(name: str, value: str) -> None:
    """Запись секрета репозитория через API (PyNaCl sealed box), без argv/логов."""
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


# --------------------------------------------------------------------- hirify

async def main() -> int:
    mail = TempMail()
    hirify_pass = _rand(18)

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

        section("0. CSRF-COOKIE (Laravel Sanctum)")
        r = await c.get(f"{API}/sanctum/csrf-cookie")
        print(f"  GET {API}/sanctum/csrf-cookie -> HTTP {r.status_code}")
        xsrf = c.cookies.get("XSRF-TOKEN", "")
        print(f"  XSRF-TOKEN: {'получен' if xsrf else 'НЕТ'}")
        auth_headers = {"X-XSRF-TOKEN": unquote(xsrf)} if xsrf else {}

        section("1. ВРЕМЕННЫЙ ЯЩИК")
        await mail.ensure()

        section("2. POST /auth/register/start")
        r = await c.post(f"{API}/auth/register/start", json={
            "name": ACCOUNT_NAME,
            "email": mail.address,
            "password": hirify_pass,
            "password_confirmation": hirify_pass,
            "locale": "ru",
            "auth_source": "site",
        }, headers=auth_headers)
        print(f"  HTTP {r.status_code}")
        if r.status_code not in (200, 201):
            print(f"  ответ: {r.text[:400]}")
            raise RuntimeError("register/start не прошёл")
        rt = r.json()["registration_token"]
        print("  registration_token получен")

        section("3. OTP ИЗ ПИСЬМА")
        otp = await mail.wait_otp()

        section("4. POST /auth/register/verify")
        xsrf = c.cookies.get("XSRF-TOKEN", "")
        auth_headers = {"X-XSRF-TOKEN": unquote(xsrf)} if xsrf else {}
        r = await c.post(f"{API}/auth/register/verify", json={
            "registration_token": rt,
            "otp": otp,
        }, headers=auth_headers)
        print(f"  HTTP {r.status_code}")
        if r.status_code not in (200, 201):  # Laravel: 201 = Created
            print(f"  ответ: {r.text[:400]}")
            raise RuntimeError("register/verify не прошёл")
        user = r.json()
        print(f"  пользователь: id={user.get('id')}, email={user.get('email', '')[:3]}***")

        cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
        domains = sorted({c2.domain for c2 in c.cookies.jar})
        print(f"  cookie: {len(cookie)} символов; ключи: {sorted(c.cookies.keys())}")
        print(f"  домены cookie: {domains}")

        section("5. ПРОВЕРКА: контакты видны с этой cookie?")
        # страницу вакансии читаем на САЙТЕ (hirify.me), cookie шлём вручную —
        # как это делает бот (HirifyClient подставляет сырую строку Cookie)
        r = await c.get(JOB_TG_SOURCE, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": SITE + "/",
            "Cookie": cookie,
        })
        print(f"  GET вакансия (tg-источник) -> HTTP {r.status_code}")
        v = parse_job_page(r.text, JOB_TG_SOURCE)
        print(f"  парсер: заголовок={v.title!r}")
        print(f"  описание: {len(v.description)} символов; компания={v.company!r}")
        print(f"  contacts_hidden={v.contacts_hidden}, has_direct_contact={v.has_direct_contact}")
        for ct in v.contacts:
            print(f"    • [{ct.kind}] {ct.display} ({ct.source})")

        r2 = await c.get(FEED_URL, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": SITE + "/",
            "Cookie": cookie,
        })
        links = extract_job_links(r2.text, base_url=SITE)
        print(f"  фид с фильтром: HTTP {r2.status_code}, вакансий: {len(links)}")
        for job_id, slug, _ in links[:3]:
            print(f"    {job_id}  {slug}")

        verdict_ok = v.has_direct_contact and not v.contacts_hidden
        print(f"\nVERDICT_CONTACTS: {'OK — контакт рекрутера виден' if verdict_ok else 'НЕ ВИДЕН'}")

        section("6. СЕКРЕТЫ РЕПОЗИТОРИЯ")
        set_gh_secret("HIRIFY_COOKIE", cookie)
        set_gh_secret("HIRIFY_EMAIL", mail.address)
        set_gh_secret("HIRIFY_PASSWORD", hirify_pass)

    await mail.delete()
    print("\nГОТОВО: бот теперь ходит на hirify.me авторизованно.")
    if not verdict_ok:
        print("ВНИМАНИЕ: cookie есть, но контакт не найден — см. лог выше.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
