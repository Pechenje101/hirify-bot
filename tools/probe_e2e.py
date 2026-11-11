"""Сквозной живой тест: фид → API вакансии → контакты → рендер карточки.

Запускается на GitHub Actions (probe.yml) с секретом HIRIFY_COOKIE:
собирает один настоящий проход мониторинга (без отправки в Telegram) и
печатает, что получилось — вакансии, контакты, фрагмент карточки.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cards import render_rich_html  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.hirify import HirifyClient, HirifyRestrictedError  # noqa: E402
from app.parsing import vacancy_from_api  # noqa: E402


async def main() -> int:
    cfg = load_config()
    client = HirifyClient(cfg)
    await client.open()
    db = Database(Path(tempfile.mkdtemp(prefix="e2e_")) / "e2e.db")
    try:
        print(f"cookie: {'есть' if client.has_cookie else 'НЕТ'}")
        items = await client.fetch_feed_api(cfg.feeds[0], 1)
        print(f"фид «{cfg.feeds[0].label}» (API, только прямые TG-контакты): {len(items)} вакансий")
        for it in items[:5]:
            print(f"    {it.get('id')}  {it.get('slug')}  [source={it.get('source')}]")

        ok = bad = 0
        for it in items[:2]:  # щадяще: анти-бот сайта реагирует на серии запросов
            full = str(it.get("slug"))
            try:
                data = await client.fetch_vacancy(full)
                contacts = await client.fetch_contacts(full)
            except HirifyRestrictedError as e:
                print(f"\n== {full} ==\n  контакты за премиумом (403) — пропускаем: {e}")
                bad += 1
                continue
            v = vacancy_from_api(data, contacts, cfg.feeds[0].key, cfg.feeds[0].label)
            got = v.has_direct_contact
            db.add_job(int(it.get("id") or 0), full, f"https://hirify.me/jobs/{full}")
            print(f"\n== {full} ==")
            print(f"  {v.title!r} · {v.company or '—'} · {v.salary or '—'}")
            print(f"  формат: {v.work_format or '—'} | занятость: {v.employment_type or '—'}")
            print(f"  описание: {len(v.description)} симв. | теги: {v.skills[:6]}")
            if v.contacts:
                for c in v.contacts[:3]:
                    print(f"  контакт: [{c.kind}] {c.url}")
            else:
                print("  контакт: НЕТ")
            if got:
                card = render_rich_html(v)  # автобюджет: полное описание
                print(f"  карточка rich: {len(card)} симв.; фрагмент: {card[:120]!r}")
                ok += 1
            else:
                bad += 1
        print(f"\nVERDICT_E2E: {ok} с контактом из {ok + bad}; cookie {'РАБОТАЕТ' if ok else 'НЕ РАБОТАЕТ'}")
        return 0
    finally:
        await client.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
