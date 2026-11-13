"""Живая проба источников-ТГ-каналов: читается ли t.me/s/<имя> и что там.

Проверяет ровно тот путь, которым работает бот в бою:
  ChannelClient.fetch_posts(name) → GET https://t.me/s/<имя> (curl_cffi,
  имперсонация Chrome) → parse_channel_html → [{id, created_at, text}].

Печатает по каждому каналу из TG_CHANNELS (или CLI-аргумента):
  • сколько постов прочитано;
  • id/дату/начало текста первого и последнего поста (виден ли parse_vacancy-кандидат);
  • сколько постов прошло бы детектор looks_like_vacancy;
  • пример channel_job_id.

Запускать с GitHub Actions (probe-channels.yml) — из песочницы разработки
t.me недоступен. Сеть и чтение АНОНИМНЫЕ: ни cookie, ни Telegram-аккаунта.

CLI: python tools/probe_channels.py [имя1 имя2 …]  (по умолчанию — TG_CHANNELS).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels import ChannelClient, channel_job_id, parse_channel_html  # noqa: E402
from app.config import load_config  # noqa: E402
from app.textparse import looks_like_vacancy  # noqa: E402

DEFAULT_CHANNELS = "cpa_people,opento_igaming,payments_lenkep,IT_iGaming"


def _brief(text: str, n: int = 90) -> str:
    s = " ".join(text.split())
    return s[:n] + ("…" if len(s) > n else "")


async def main() -> int:
    cfg = load_config()
    names = sys.argv[1:] or (cfg.tg_channels or DEFAULT_CHANNELS).split(",")
    names = [n.strip() for n in names if n.strip()]
    print(f"каналы: {', '.join(names)}; имперсонация: {cfg.impersonate_target}")
    client = ChannelClient(cfg)
    await client.open()
    failures = 0
    try:
        for name in names:
            try:
                html, status, final_url = await client.fetch_page(name)
            except Exception as e:  # noqa: BLE001
                print(f"\n[{name}] ❌ ОШИБКА ЗАГРУЗКИ: {type(e).__name__}: {str(e)[:140]}")
                failures += 1
                continue
            posts = parse_channel_html(html)
            detected = sum(1 for p in posts if looks_like_vacancy(p["text"]))
            print(f"\n[{name}] HTTP {status}, {len(html)} байт HTML, финальный URL: {final_url}")
            print(f"  вхождений разметки: wrap={html.count('tgme_widget_message_wrap')} "
                  f"text_div={html.count('tgme_widget_message_text')} "
                  f"data-post={html.count('data-post=')}")
            print(f"  постов прочитано: {len(posts)}; похоже на вакансии: {detected}")
            if not posts:
                # сниппет начала body — видно, что реально вернул сервер
                import re as _re
                body = _re.sub(r"\s+", " ", html[:600])
                print(f"  сниппет: {body[:400]}")
            for tag, p in (("свежий", posts[0] if posts else None),
                           ("последний", posts[-1] if posts else None)):
                if not p:
                    continue
                jid = channel_job_id(name, p["id"])
                print(f"  {tag}: id={p['id']} job_id={jid} created_at={p['created_at']}")
                print(f"        {_brief(p['text'])}")
            await asyncio.sleep(1.5)
    finally:
        await client.close()
    print("\nИТОГ:", "все каналы читаются и разбираются" if not failures else f"проблемных каналов: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
