"""Батч-проба каналов-кандидатов: живость + проходимость конвейера.

Для каждого имени канала: читаем t.me/s (анонимно, вежливая пауза),
считаем: постов прочитано, дата последнего, сколько похоже на вакансию,
у скольких есть прямой контакт (по производственному фильтру бота).
Вывод — таблица для решения «добавлять ли в TG_CHANNELS».

Запуск: python tools/probe_candidates.py name1 name2 ...
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.channels import ChannelClient  # noqa: E402
from app.config import Config  # noqa: E402
from app.textparse import looks_like_vacancy, parse_vacancy_text  # noqa: E402


async def probe(client: ChannelClient, name: str) -> dict:
    out = {"name": name, "posts": 0, "last": "", "vac": 0, "contact": 0,
           "sample": "", "err": ""}
    try:
        posts = await client.fetch_posts(name)
    except Exception as e:  # noqa: BLE001 — канал не читается, это и есть результат
        out["err"] = f"{type(e).__name__}: {e}"[:80]
        return out
    out["posts"] = len(posts)
    if not posts:
        out["err"] = "0 постов (не публичный/пустой)"
        return out
    out["last"] = str(posts[-1].get("created_at") or "")[:10]
    for p in posts:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        if not looks_like_vacancy(t):
            continue
        out["vac"] += 1
        v = parse_vacancy_text(t)
        if v.has_direct_contact:
            out["contact"] += 1
            if not out["sample"]:
                out["sample"] = (v.title or "?")[:40]
    return out


async def main() -> None:
    names = [a.strip() for a in sys.argv[1:] if a.strip()]
    if not names:
        print("нет имён каналов")
        return
    client = ChannelClient(Config())
    print(f"{'канал':<24} {'постов':>6} {'последн.':>10} {'вак':>4} {'конт':>5}  ошибка/пример")
    print("-" * 100)
    for n in names:
        r = await probe(client, n)
        tail = r["err"] or r["sample"]
        print(f"{r['name']:<24} {r['posts']:>6} {r['last']:>10} "
              f"{r['vac']:>4} {r['contact']:>5}  {tail}")
        await asyncio.sleep(1.5)  # вежливость к t.me
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
