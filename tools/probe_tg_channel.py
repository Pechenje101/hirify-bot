"""Быстрая проба канала-источника: читается ли t.me/s/<имя> и что там за посты.

Использование: python tools/probe_tg_channel.py <имя_канала> [маркер]
Второй аргумент — подстрока для фильтрации постов (диагностика «есть ли
там вообще вакансии»), пусто = показать все.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.channels import ChannelClient  # noqa: E402
from app.config import Config  # noqa: E402


async def main() -> None:
    name = sys.argv[1].strip()
    marker = (sys.argv[2] if len(sys.argv) > 2 else "").strip().lower()
    client = ChannelClient(Config())
    posts = await client.fetch_posts(name)
    await client.close()
    print(f"канал {name}: {len(posts)} постов прочитано")
    for p in posts[-8:]:
        text = (p.get("text") or "").replace("\n", " ⏎ ")
        if marker and marker not in text.lower():
            continue
        print(f"--- [{p.get('id')}] {p.get('datetime', '')[:16]}")
        print(f"    {text[:400]}")


if __name__ == "__main__":
    asyncio.run(main())
