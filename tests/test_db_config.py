"""Тесты SQLite-хранилища и конфигурации."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config, FEED_PRESETS, parse_feeds  # noqa: E402
from app.db import Database  # noqa: E402


def test_feed_urls():
    cfg = Config(bot_token="x")
    feeds = cfg.feeds
    assert [f.key for f in feeds] == ["remote", "global", "crypto"]
    remote = feeds[0]
    assert remote.base_url == "https://hirify.me/remote-it-jobs"
    assert remote.label == "🏠 Удалёнка"

    url = cfg.feed_url_for_page(remote, 3)
    assert url.startswith("https://hirify.me/remote-it-jobs?")
    assert "only_direct_contact=1" in url and "contact_types=telegram" in url
    assert "sort_by=newest" in url
    assert url.endswith("&page=3")

    # page из пользовательских параметров исключается, наш добавляется
    cfg2 = Config(bot_token="x", feed_params="only_direct_contact=1&page=9")
    u2 = cfg2.feed_url_for_page(cfg2.feeds[0], 2)
    assert "page=9" not in u2
    assert u2.endswith("&page=2")


def test_parse_feeds():
    # пресеты
    assert [f.key for f in parse_feeds("remote,crypto")] == ["remote", "crypto"]
    # дубликаты выкидываются
    assert [f.key for f in parse_feeds("remote,remote,global")] == ["remote", "global"]
    # пробелы и слэши не мешают
    assert [f.key for f in parse_feeds(" remote , crypto ")] == ["remote", "crypto"]
    # пусто → дефолтные разделы
    assert [f.key for f in parse_feeds("")] == ["remote", "global", "crypto"]
    # «none» (и любой off-токен) — hirify-источники выключены: пустой список
    # БЕЗ отката на пресеты; глушит и смешанный список
    assert parse_feeds("none") == []
    assert parse_feeds("OFF") == []
    assert parse_feeds(" none , remote") == []
    # свой лендинг
    f = parse_feeds("/fintech-jobs")[0]
    assert f.base_url == "https://hirify.me/fintech-jobs"
    # полный URL
    f2 = parse_feeds("https://hirify.me/web3-crypto-jobs")[0]
    assert f2.base_url.endswith("web3-crypto-jobs")
    # пресеты на месте
    assert FEED_PRESETS["global"].base_url == "https://hirify.me/remote-jobs"
    assert FEED_PRESETS["crypto"].base_url.endswith("web3-crypto-jobs")
    assert FEED_PRESETS["crypto_remote"].base_url.endswith("web3-crypto-remote-jobs")


def test_hirify_feeds_off(monkeypatch):
    """HIRIFY_FEEDS=none — сайт заморожен: в источниках только ТГ-каналы."""
    monkeypatch.setenv("HIRIFY_FEEDS", "none")
    monkeypatch.setenv("TG_CHANNELS", "cpa_people")
    cfg = Config(bot_token="x")
    assert [f.source for f in cfg.feeds] == ["tg"]
    # и вовсе без источников — бот жив, просто опрашивать нечего
    monkeypatch.setenv("TG_CHANNELS", "")
    cfg2 = Config(bot_token="x")
    assert cfg2.feeds == []


def test_db_lifecycle(tmp=None):
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        # WAL-режим включён (устойчивость к сбоям + быстрые коммиты)
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

        db.add_job(1, "slug-1", "https://hirify.me/jobs/1-slug-1", title="T", company="C")
        db.add_job(1, "slug-1", "https://hirify.me/jobs/1-slug-1")  # дубль игнорируется
        db.add_job(2, "slug-2", "https://hirify.me/jobs/2-slug-2")

        # свежедобавленные ('new') ещё не финализированы — ретраятся
        assert db.known_job_ids([1, 2, 3]) == set()
        assert db.job_status(1) == "new"
        db.mark_job(1, "sent")
        db.mark_job(2, "skipped_no_contact")
        assert db.known_job_ids([1, 2, 3]) == {1, 2}

        db.update_job_parsed(1, title="T2", tg_contacts=[{"kind": "username", "value": "hr"}])
        db.mark_job(1, "sent")
        assert db.job_status(1) == "sent"
        db.mark_job(2, "skipped_no_contact")
        db.add_job(3, "slug-3", "https://hirify.me/jobs/3-slug-3")
        db.mark_job(3, "skipped_not_crypto")

        s = db.stats()
        assert s["sent"] == 1 and s["skipped_no_contact"] == 1 and s["total"] == 3
        assert s["filtered"] == 1

        db.add_subscriber(100, "@u")
        db.add_subscriber(100, "@u")  # дубль
        db.add_subscriber(200, None)
        assert db.subscriber_ids() == [100, 200]
        db.remove_subscriber(100)
        assert db.subscriber_ids() == [200]

        db.set_setting("paused", "1")
        assert db.get_setting("paused") == "1"
        db.set_setting("paused", "0")
        assert db.get_setting("paused", "def") == "0"
        assert db.get_setting("nokey", "d") == "d"
        db.close()


def test_retryable_statuses_are_not_known():
    """send_failed/fetch_failed/new — не финальные: known_job_ids их не возвращает.

    'new' — вакансия добавлена в проход, который оборвался до mark_job
    (рестарт GitHub Actions посреди прохода): её обязан ретраить,
    иначе она молча потеряется навсегда.
    """
    from app.db import RETRYABLE_STATUSES

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        db.add_job(1, "s1", "u1")
        db.add_job(2, "s2", "u2")
        db.add_job(3, "s3", "u3")
        db.add_job(4, "s4", "u4")
        db.mark_job(1, "sent")
        db.mark_job(2, "send_failed")
        db.mark_job(3, "fetch_failed")
        # 4 остался 'new' — как при рестарте посреди прохода

        assert db.known_job_ids([1, 2, 3, 4]) == {1}  # ретраятся 2, 3 и 4
        assert RETRYABLE_STATUSES == ("send_failed", "fetch_failed", "new")
        # финальные статусы ретраить не надо
        db.mark_job(4, "skipped_no_contact")
        assert db.known_job_ids([4]) == {4}

        # счётчик попыток
        assert db.job_attempts(2) == 0
        assert db.bump_job_attempts(2) == 1
        assert db.bump_job_attempts(2) == 2
        assert db.job_attempts(2) == 2
        assert db.job_attempts(999) == 0  # нет такой вакансии

        # статистика видит оба статуса
        db.mark_job(3, "send_failed")
        s = db.stats()
        assert s["send_failed"] == 2 and s["fetch_failed"] == 0
        db.close()


def test_known_job_ids_chunking():
    """Больше 400 id — запрос идёт чанками и ничего не теряет."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        n = 850
        for i in range(n):
            db.add_job(i + 1, f"s{i}", f"u{i}")
            db.mark_job(i + 1, "sent")  # финальный статус — «известна»
        ids = db.known_job_ids(list(range(1, n + 1)))
        assert ids == set(range(1, n + 1))
        # вперемешку с несуществующими
        assert db.known_job_ids([1, n, 10 ** 9]) == {1, n}
        # чанки не путают подстановку статусов-ретраев
        db.add_job(10 ** 8, "sx", "ux")
        assert db.known_job_ids([10 ** 8]) == set()
        db.close()


def test_config_env(monkeypatch=None):
    os.environ["HIRIFY_FEED_PARAMS"] = "countries=ru&only_direct_contact=1"
    os.environ["POLL_MINUTES"] = "7"
    os.environ["FAST_POLL_SEC"] = "45"
    os.environ["USE_RICH"] = "0"
    os.environ["USE_WIDGET"] = "0"
    os.environ["HIRIFY_FEEDS"] = "crypto,global"
    os.environ["REMOTE_STRICT"] = "0"
    os.environ["INDUSTRY_STRICT_TG"] = "1"
    os.environ["TG_CHANNELS"] = "cpa_people,https://t.me/s/payments_lenkep"
    from app.config import Config as C
    cfg = C()
    assert cfg.feed_params == "countries=ru&only_direct_contact=1"
    assert cfg.poll_minutes == 7.0
    assert cfg.fast_poll_sec == 45.0
    assert cfg.use_rich is False
    assert cfg.use_widget is False
    # источники = разделы hirify + каналы (в заданном порядке)
    assert [f.key for f in cfg.feeds] == ["crypto", "global",
                                          "tg_cpa_people", "tg_payments_lenkep"]
    assert cfg.feeds[2].source == "tg" and cfg.feeds[2].channel == "cpa_people"
    assert cfg.remote_strict is False and cfg.crypto_strict is True
    assert cfg.industry_strict_tg is True
    for k in ("HIRIFY_FEED_PARAMS", "POLL_MINUTES", "FAST_POLL_SEC", "USE_RICH",
              "USE_WIDGET", "HIRIFY_FEEDS", "REMOTE_STRICT", "INDUSTRY_STRICT_TG",
              "TG_CHANNELS"):
        os.environ.pop(k, None)


def test_config_defaults_for_fast_delivery():
    """Дефолты под «карточка приходит сразу»: 5 минут базовый интервал + 2 мин повтор."""
    for k in ("POLL_MINUTES", "FAST_POLL_SEC"):
        os.environ.pop(k, None)
    from app.config import Config as C
    cfg = C()
    assert cfg.poll_minutes == 5.0
    assert cfg.fast_poll_sec == 120.0


def test_subscriber_settings_roundtrip():
    """Настройки рассылки на подписчика: сохранение, чтение, сброс на дефолт."""
    import json

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        assert db.subscriber(1) is None
        db.add_subscriber(1, "@a")
        db.add_subscriber(2, "@b")
        # по умолчанию: feeds=None («как у всех»), silent=False, display=auto
        assert db.subscriber(1) == {
            "chat_id": 1, "username": "@a", "feeds": None,
            "silent": False, "display": "auto",
        }
        # выбрали разделы + тихий режим + пузырь
        db.update_subscriber_settings(1, feeds=["crypto", "remote"], silent=True, display="bubble")
        s = db.subscriber(1)
        assert s["feeds"] == {"remote", "crypto"} and s["silent"] is True
        assert s["display"] == "bubble"
        # в БД лежит отсортированный JSON
        raw = db.conn.execute("SELECT feeds FROM subscribers WHERE chat_id=1").fetchone()["feeds"]
        assert json.loads(raw) == ["crypto", "remote"]
        # пустой список = «ничего не присылать» — НЕ то же самое, что дефолт
        db.update_subscriber_settings(2, feeds=[], silent=False, display="classic")
        assert db.subscriber(2)["feeds"] == set()
        assert db.subscriber(2)["display"] == "classic"
        # сброс на «по умолчанию»
        db.update_subscriber_settings(1, feeds=None, silent=False, display="auto")
        assert db.subscriber(1)["feeds"] is None
        # мусорный вид карточек превращается в auto
        db.update_subscriber_settings(2, feeds=[], silent=False, display="неизвестный-режим")
        assert db.subscriber(2)["display"] == "auto"
        # settings для всех сразу
        subs = db.subscriber_settings()
        assert {x["chat_id"] for x in subs} == {1, 2}
        assert all("display" in x for x in subs)
        # update сам создаёт подписчика при необходимости
        db.update_subscriber_settings(3, feeds=["remote"], silent=True, display="rich")
        assert db.subscriber(3)["feeds"] == {"remote"}
        assert db.subscriber(3)["display"] == "rich"
        db.close()


def test_legacy_db_without_subscriber_settings_is_migrated():
    """Старая база без feeds/silent/display у подписчиков мигрирует без потери данных."""
    import sqlite3

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "old.db"
        conn = sqlite3.connect(p)
        conn.executescript(
            """
            CREATE TABLE jobs (
                job_id INTEGER PRIMARY KEY, slug TEXT, url TEXT, title TEXT,
                company TEXT, salary TEXT, tg_contacts TEXT, first_seen TEXT,
                sent_at TEXT, status TEXT NOT NULL DEFAULT 'new'
            );
            CREATE TABLE subscribers (
                chat_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.execute("INSERT INTO subscribers VALUES (555, '@legacy', '2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        db = Database(p)  # миграция не должна упасть
        s = db.subscriber(555)
        assert s["username"] == "@legacy" and s["feeds"] is None and s["silent"] is False
        assert s["display"] == "auto"  # колонка display добавлена с дефолтом
        db.close()


def test_prune_jobs():
    """Гигиена БД: старьё (включая зависшие ретраи) удаляется, свежее остаётся."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        # свежие вакансии
        for i in (1, 2, 3):
            db.add_job(i, f"slug-{i}", f"https://hirify.me/jobs/{i}-slug-{i}")
            db.mark_job(i, "sent")
        # старьё: first_seen задвинут в прошлое (sent и зависший send_failed)
        for i in (4, 5):
            db.add_job(i, f"slug-{i}", f"https://hirify.me/jobs/{i}-slug-{i}")
        db.mark_job(4, "sent")
        db.mark_job(5, "send_failed")  # зависший ретрай — тоже мусор в этом возрасте
        with db.conn:  # прямой доступ в тесте: задвинуть время появления
            db.conn.execute("UPDATE jobs SET first_seen='2020-01-01T00:00:00+00:00' WHERE job_id IN (4,5)")

        removed = db.prune_jobs(35)
        assert removed == 2
        assert db.job_status(4) is None and db.job_status(5) is None
        assert db.job_status(1) == "sent"  # свежие не тронуты
        assert db.known_job_ids([1, 2, 3]) == {1, 2, 3}  # дедуп жив

        # повторная чистка в тот же день — удалять больше нечего
        assert db.prune_jobs(35) == 0

        # файл пережил VACUUM и не побился
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

        # days=0 — чистка выключена: даже явный мусор не трогаем
        db.add_job(6, "slug-6", "https://hirify.me/jobs/6-slug-6")
        with db.conn:
            db.conn.execute("UPDATE jobs SET first_seen='2020-01-01T00:00:00+00:00' WHERE job_id=6")
        assert db.prune_jobs(0) == 0
        assert db.job_status(6) is not None
        db.close()


def test_prune_reclaims_file_space():
    """VACUUM в WAL кладёт сжатую копию в -wal: без wal_checkpoint(TRUNCATE)
    основной .db не уменьшается (774 КБ на диске при 6 страницах контента) —
    а именно его бот коммитит в git. Чистка обязана возвращать место файлу.
    """
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.db"
        db = Database(p)
        for i in range(1500):
            db.add_job(i, f"slug-{i}", f"https://hirify.me/jobs/{i}-slug-{i}", title="T" * 300)
        with db.conn:
            db.conn.execute("UPDATE jobs SET first_seen='2020-01-01T00:00:00+00:00'")
        before = p.stat().st_size
        removed = db.prune_jobs(35)
        assert removed == 1500
        after = p.stat().st_size
        assert after < before / 2          # место вернулось ОСНОВНОМУ файлу
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.close()
