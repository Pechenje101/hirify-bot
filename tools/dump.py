"""Диагностика парсера hirify.me — запускайте у себя, где сайт доступен.

Примеры:
    python tools/dump.py feed                     # первый раздел из HIRIFY_FEEDS
    python tools/dump.py feed remote              # раздел remote (удалёнка)
    python tools/dump.py feed crypto 2            # раздел crypto, страница 2
    python tools/dump.py job 1261754-kotlin-parser-developer
    python tools/dump.py job https://hirify.me/jobs/1261754-kotlin-parser-developer
    python tools/dump.py demo                 # проверить парсер на встроенном примере

Разделы: remote (Удалёнка), global (Глобальная удалёнка), crypto (Crypto & Web3),
crypto_remote, all — или путь лендинга ('/some-jobs').

Что делает:
  * скачивает страницу (с cookie из .env / HIRIFY_COOKIE);
  * сохраняет HTML в debug/ (посмотрите глазами, если что-то не парсится);
  * печатает результат работы парсера: вакансии, поля, TG-контакты.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config, FEED_PRESETS, parse_feeds  # noqa: E402
from app.hirify import HirifyClient  # noqa: E402
from app.parsing import extract_job_links, parse_job_page  # noqa: E402

DEMO_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><title>Senior Python Developer — FooBar — Hirify</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Senior Python Developer",
 "description":"Ищем сеньора в команду ядра.\\n\\nПишите @foobar_hr в Telegram.",
 "datePosted":"2026-11-10","employmentType":"FULL_TIME",
 "hiringOrganization":{"@type":"Organization","name":"FooBar"},
 "baseSalary":{"@type":"MonetaryAmount","currency":"USD",
   "value":{"@type":"QuantitativeValue","minValue":90000,"maxValue":130000,"unitText":"YEAR"}},
 "jobLocationType":"TELECOMMUTE"}
</script></head>
<body><main><h1>Senior Python Developer</h1>
<div class="job-contacts">
  <a href="https://t.me/foobar_hr">@foobar_hr</a> — рекрутер,
  оригинал: <a href="https://t.me/foobar_jobs/123">канал</a>
</div>
<div>Source - Telegram channel. Name available after authorization</div>
</main></body></html>"""


def show_vacancy(v) -> None:
    print(f"  job_id:        {v.job_id}")
    print(f"  заголовок:     {v.title!r}")
    print(f"  компания:      {v.company!r}")
    print(f"  зарплата:      {v.salary!r}")
    print(f"  формат:        {v.work_format!r} {v.location!r}")
    print(f"  грейд/тип:     {v.grade!r} / {v.employment_type!r}")
    print(f"  раздел:        {v.feed_label!r}")
    print(f"  крипто:        {v.is_crypto}")
    print(f"  дата:          {v.date_posted!r}")
    print(f"  навыки:        {v.skills[:10]}")
    print(f"  описание:      {len(v.description)} символов")
    print(f"  источник:      {v.source_type!r}, контакты скрыты: {v.contacts_hidden}")
    print(f"  прямой контакт: {v.has_direct_contact}")
    for c in v.contacts:
        print(f"    • [{c.kind}] {c.display} -> {c.url} (найден в: {c.source})")


async def main() -> int:
    cfg = load_config()
    cfg.debug_dump_dir.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

    if cmd == "demo":
        print("== Проверка парсера на встроенном примере ==")
        v = parse_job_page(DEMO_PAGE, "https://hirify.me/jobs/555-demo")
        show_vacancy(v)
        return 0

    client = HirifyClient(cfg)
    await client.open()
    try:
        if cmd == "feed":
            args2 = args[1:]
            # 'feed 2' → страница 2 первого раздела; 'feed crypto 2' → раздел crypto, стр. 2
            if args2 and args2[0].isdigit():
                feed, page = cfg.feeds[0], int(args2[0])
            elif args2:
                key = args2[0].lstrip("/")
                feed = parse_feeds(key)[0] if key not in FEED_PRESETS else FEED_PRESETS[key]
                page = int(args2[1]) if len(args2) > 1 and args2[1].isdigit() else 1
            else:
                feed, page = cfg.feeds[0], 1
            print(f"== Фид «{feed.label}»: {cfg.feed_url_for_page(feed, page)} ==")
            print(f"   cookie: {'есть' if client.has_cookie else 'НЕТ (контакты будут скрыты!)'}")
            html = await client.fetch_feed(feed, page)
            (cfg.debug_dump_dir / f"feed_{feed.key}_p{page}.html").write_text(html, encoding="utf-8")
            links = extract_job_links(html, base_url=cfg.hirify_base)
            print(f"   найдено вакансий на странице: {len(links)}")
            for job_id, slug, url in links[:25]:
                print(f"     {job_id:>9}  {slug}")
            print(f"   HTML сохранён: {cfg.debug_dump_dir / f'feed_{feed.key}_p{page}.html'}")
            return 0

        if cmd == "job":
            target = args[1] if len(args) > 1 else ""
            if not target:
                print("Укажите id-slug или полный URL вакансии")
                return 2
            url = target if target.startswith("http") else f"{cfg.hirify_base}/jobs/{target}"
            print(f"== Вакансия: {url} ==")
            print(f"   cookie: {'есть' if client.has_cookie else 'НЕТ (контакты будут скрыты!)'}")
            html = await client.fetch_job(url)
            (cfg.debug_dump_dir / "job.html").write_text(html, encoding="utf-8")
            v = parse_job_page(html, url)
            show_vacancy(v)
            print(f"   HTML сохранён: {cfg.debug_dump_dir / 'job.html'}")
            return 0

        print(__doc__)
        return 2
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
