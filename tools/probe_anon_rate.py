"""Замер реального процента попаданий анонимного пути (ANON_FIRST).

Берёт первые N вакансий фида (анонимно), для каждой читает публичную
страницу БЕЗ cookie и прогоняет через боевой parse_job_page:

    python tools/probe_anon_rate.py [N]        # N вакансий, по умолчанию 15

Печатает по каждой: длину JSON-LD-описания (обрезка hirify видна как
хвост «…Telegram:» без хэндла), нашёлся ли прямой контакт, откуда.
Итог — % доставки без единого auth-запроса. Read-only, риска для
аккаунта ноль: cookie не используется вовсе.

Запускать там, где hirify.me доступен (GitHub Actions — workflow probe).
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.hirify import HirifyClient  # noqa: E402
from app.parsing import parse_job_page  # noqa: E402


async def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    cfg = load_config()
    client = HirifyClient(cfg)
    await client.open()
    hits = 0
    total = 0
    try:
        items = await client.fetch_feed_api(cfg.feeds[0], 1)
        print(f"фид «{cfg.feeds[0].key}»: {len(items)} элементов, замеряем первые {n}")
        for it in items[:n]:
            full_slug = str(it.get("slug") or "")
            job_id = it.get("id")
            if not full_slug or not job_id:
                continue
            url = f"{cfg.hirify_base}/jobs/{full_slug}"
            total += 1
            try:
                html = await client.fetch_job(url)
                v = parse_job_page(html, url)
                v.feed_key, v.feed_label = cfg.feeds[0].key, cfg.feeds[0].label
            except Exception as e:  # noqa: BLE001
                print(f"  [{job_id}] лендинг не отдался: {e}")
                continue
            desc_len = len(v.description or "")
            src = ""
            if v.has_direct_contact:
                hits += 1
                p = v.primary_contact
                src = f"{p.kind}:{p.value} (source={p.source})"
            # признак обрезки: описание кончается «…:»/«…-» без хэндла
            tail = (v.description or "")[-60:].replace("\n", " ")
            truncated = "да" if re.search(r"[:\-\s]\s*$", tail) and not src else "—"
            print(f"  [{job_id}] desc={desc_len:>5} sym, контакт={'ЕСТЬ ' if src else 'нет'}, "
                  f"обрезка={truncated}, {src or tail!r}")
            await asyncio.sleep(1.0)
    finally:
        await client.close()
    rate = 100.0 * hits / total if total else 0.0
    print(f"\nИТОГ: {hits}/{total} ({rate:.0f}%) вакансий доставимо анонимно, "
          f"без единого запроса аккаунта")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
