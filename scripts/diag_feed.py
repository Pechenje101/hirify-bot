"""Диагностика «почему автомониторинг молчит»: воронка фида → фильтры бота.

Запускается воркфлоу diag.yml (Actions, cookie из секретов). Отвечает:
  1) жив ли cookie (фид и GET вакансии принимаются?);
  2) сколько вакансий сейчас в фидах (только прямые TG-контакты, newest,
     страница 1 — ровно то, что видит бот при FEED_PAGES=1);
  3) сколько из них прошло бы индустриальный фильтр (iGaming/гемблинг/беттинг/
     дейтинг/high-risk/NDA/крипта) — с ПОЛНЫМ описанием вакансии, как у бота;
  4) не стоит ли аккаунт на паузе «автоматизация» (403 automation_detected).

POST /api/vacancies/{slug}/contacts НЕ вызываем: бесплатные попытки —
ресурс, контакты фид и так гарантирует (only_direct_contact=1 на сервере).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.hirify import (  # noqa: E402
    HirifyAutomationError, HirifyAuthError, HirifyClient, HirifyError,
)
from app.parsing import CRYPTO_RE, INDUSTRY_RE, vacancy_from_api  # noqa: E402

DEEP = int(os.getenv("DIAG_DEEP", "20"))


async def main() -> int:
    cfg = load_config()
    cfg.hirify_cookie = os.environ.get("HIRIFY_COOKIE", "").strip()
    print(f"cookie: {'задан, ' + str(len(cfg.hirify_cookie)) + ' символов' if cfg.hirify_cookie else 'ПУСТО — фид/вакансии не откроются'}")
    print(f"фиды: {', '.join(f.key for f in cfg.feeds)}; параметры: {cfg.feed_params}")
    print(f"фильтры бота: industry_strict={cfg.industry_strict}, "
          f"remote_strict={cfg.remote_strict}, crypto_strict={cfg.crypto_strict}")
    print(f"глубина разбора вакансий: первые {DEEP} уникальных со страницы 1\n")

    client = HirifyClient(cfg)
    await client.open()
    links: list[tuple[int, str, str, object]] = []
    code = 0
    try:
        for feed in cfg.feeds:
            try:
                items = await client.fetch_feed_api(feed, 1)
            except HirifyAuthError as e:
                print(f"[{feed.key}] ❌ ФИД: сессия не принята ({e}) — HIRIFY_COOKIE МЁРТВ, обновите")
                code = 1
                continue
            except HirifyError as e:
                print(f"[{feed.key}] ❌ ФИД сломан: {e}")
                code = 1
                continue
            print(f"[{feed.key}] вакансий в фиде (страница 1): {len(items)}")
            if items and isinstance(items[0], dict):
                first = items[0]
                created = str(first.get("created_at") or first.get("published_at") or "?")
                print(f"    свежайшая в фиде: created_at={created}")
            for it in items:
                try:
                    job_id = int(it.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if not job_id:
                    continue
                full_slug = str(it.get("slug") or "")
                short = full_slug.split("-", 1)[1] if "-" in full_slug else full_slug
                links.append((job_id, short, full_slug, feed))

        # дедуп по всем разделам сразу — как в Monitor._poll_impl
        seen: set[int] = set()
        unique: list[tuple[int, str, str, object]] = []
        for row in links:
            if row[0] in seen:
                continue
            seen.add(row[0])
            unique.append(row)
        print(f"\nвсего вакансий во всех фидах (уникальных): {len(unique)}")

        passed: list = []
        reasons: dict[str, int] = {}
        errors = 0
        checked = 0
        print(f"—— разбор первых {min(DEEP, len(unique))} вакансий (как решает бот) ——")
        for job_id, short, full_slug, feed in unique[:DEEP]:
            try:
                data = await client.fetch_vacancy(full_slug)
            except HirifyAutomationError as e:
                print(f"  ⚠️ job {job_id}: сайт подозревает автоматизацию (403) — "
                      f"бот будет ждать кулдаун 90 мин: {str(e)[:120]}")
                errors += 1
                continue
            except HirifyAuthError as e:
                print(f"  ❌ job {job_id}: сессия не принята — cookie мёртв: {e}")
                errors += 1
                code = 1
                break
            except HirifyError as e:
                print(f"  · job {job_id}: не загрузилась: {str(e)[:120]}")
                errors += 1
                continue
            v = vacancy_from_api(data, {}, feed.key, feed.label)
            checked += 1
            # ровно последовательность фильтров Monitor._poll_impl
            reason = ""
            if feed.key in ("remote", "global", "crypto_remote") and cfg.remote_strict and v.is_non_remote:
                reason = "не удалёнка"
            elif feed.key == "crypto" and cfg.crypto_strict and not v.is_crypto:
                reason = "не крипта"
            elif cfg.industry_strict and not v.is_target_industry:
                reason = "не целевая индустрия"
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
                print(f"  ✗ [{feed.key:<5}] «{v.title[:58]}» — отфильтрована: {reason}")
            else:
                passed.append(v)
                print(f"  ✓ [{feed.key:<5}] «{v.title[:58]}» — ПРОШЛА бы в рассылку "
                      f"(контакты фид гарантирует)")

        print(f"\n—— ИТОГ воронки ——")
        print(f"в фидах: {len(unique)} уникальных вакансий (страница 1 каждого раздела)")
        print(f"разобрано полных: {checked}; ошибок/пауз: {errors}")
        print(f"прошло бы все фильтры: {len(passed)}")
        for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"   отсеяно «{r}»: {n}")
        if passed:
            print("в рассылку попали бы:")
            for v in passed:
                print(f"   ✓ «{v.title[:70]}» — {v.company[:35]}, {v.date_posted}")
        else:
            print("целевых вакансий в разборе НЕТ — тишина объясняется фильтром, "
                  "а не поломкой.")
    finally:
        await client.close()
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
