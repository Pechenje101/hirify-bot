"""Живая диагностика hirify.me БЕЗ cookie: что видно анонимному посетителю?

Запускается там, где сайт доступен (в песочнице он заблокирован — есть GitHub Actions):

    python tools/probe_live.py            # фид → первые 2 вакансии → анализ
    python tools/probe_live.py <URL вакансии>

Проверяет и печатает:
  1) фид без авторизации: сколько ссылок вакансий видно;
  2) страница вакансии: что вытащил наш парсер (контакты, contacts_hidden);
  3) сырой HTML: все t.me-ссылки (в т.ч. ссылка на оригинал в канале),
     маркеры скрытия, JSON-LD, хвост описания (контакты обычно в конце);
  4) оригинальный пост в Telegram-канале (t.me/<канал>/<id> или /s/-превью):
     есть ли там контакт рекрутера — публичный путь без авторизации hirify.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import load_config  # noqa: E402
from app.hirify import HirifyClient  # noqa: E402
from app.parsing import extract_job_links, parse_job_page  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TME_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/[A-Za-z0-9_+/%-]+", re.I)
MARKERS_RE = re.compile(r"(?i)(show contacts|available after authorization|после авторизации)")
JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def section(title: str) -> None:
    print(f"\n===== {title} =====")


async def fetch(url: str) -> tuple[int, str]:
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"},
        follow_redirects=True, timeout=30.0,
    ) as c:
        r = await c.get(url)
        return r.status_code, r.text


def raw_analysis(html: str) -> list[str]:
    """Возвращает t.me-ссылки сырого HTML (уникальные)."""
    return sorted(set(TME_RE.findall(html)))


async def probe_job(url: str, client: HirifyClient) -> None:
    section(f"ВАКАНСИЯ (анонимно): {url}")
    html = await client.fetch_job(url)
    v = parse_job_page(html, url)
    print(f"  парсер: заголовок={v.title!r}, компания={v.company!r}")
    print(f"  contacts_hidden={v.contacts_hidden}, has_direct_contact={v.has_direct_contact}")
    for c in v.contacts:
        print(f"    • [{c.kind}] {c.display} -> {c.url} (найден в: {c.source})")
    links = raw_analysis(html)
    print(f"  t.me-ссылок в сыром HTML: {len(links)}")
    for link in links[:15]:
        print(f"    {link}")
    print(f"  маркеры скрытия: {len(MARKERS_RE.findall(html))} шт.")
    m = JSONLD_RE.search(html)
    if m:
        desc = ""
        try:
            import json
            desc = (json.loads(m.group(1)) or {}).get("description", "")
        except Exception:  # noqa: BLE001
            pass
        print(f"  JSON-LD описание: {len(desc)} симв.; хвост: ...{desc[-260:]!r}")
    # контекст вокруг «Show contacts» — как устроен показ контактов
    i = html.lower().find("show contacts")
    if i >= 0:
        print(f"  контекст Show contacts: ...{html[max(0, i-200):i+300]!r}...")

    # оригинальный пост в канале: t.me/<канал>/<id> из сырого HTML
    post = next((link for link in links if re.search(r"/\d+$", link)), None)
    if post:
        section(f"ОРИГИНАЛ В КАНАЛЕ (публичное превью): {post}")
        for variant in (post, post.replace("://t.me/", "://t.me/s/")):
            try:
                code, page = await fetch(variant)
                found = sorted(set(TME_RE.findall(page)))
                mentions = re.findall(r"@[A-Za-z][A-Za-z0-9_]{3,31}", page)
                print(f"  {variant} -> HTTP {code}, {len(page)} байт, "
                      f"t.me-ссылок: {len(found)}, @упоминаний: {len(mentions)}")
                for link in found[:10]:
                    print(f"    t.me: {link}")
                for mm in sorted(set(mentions))[:10]:
                    print(f"    mention: {mm}")
            except Exception as e:  # noqa: BLE001
                print(f"  {variant} -> ошибка: {e}")


async def main() -> int:
    cfg = load_config()
    client = HirifyClient(cfg)
    await client.open()
    try:
        arg = sys.argv[1] if len(sys.argv) > 1 else ""
        if arg.startswith("http"):
            await probe_job(arg, client)
            return 0

        section(f"ФИД (анонимно): {cfg.feed_url_for_page(cfg.feeds[0], 1)}")
        print(f"  cookie: {'есть' if client.has_cookie else 'НЕТ — проверяем анонимный доступ'}")
        html = await client.fetch_feed(cfg.feeds[0], 1)
        links = extract_job_links(html, base_url=cfg.hirify_base)
        print(f"  ссылок вакансий видно без авторизации: {len(links)}")
        for job_id, slug, url in links[:5]:
            print(f"    {job_id:>9}  {slug}")

        for _, _, url in links[:2]:
            await probe_job(url, client)
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
