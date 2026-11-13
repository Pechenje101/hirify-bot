"""SQLite-хранилище: дедупликация вакансий, подписчики, настройки.

Надёжность: WAL-журнал (переживает сбои, быстрее коммиты), busy_timeout,
потокобезопасность через один RLock. Ретраи: вакансии со статусом
send_failed/fetch_failed считаются «ещё не обработанными» — известные id
(known_job_ids) их не возвращает, поэтому монитор повторит попытку в
следующем проходе (ограничено счётчиком attempts).

Статус 'new' тоже не финальный: он означает «добавлена в этом проходе, но
обработка оборвалась» (например, рестарт GitHub Actions посреди прохода).
Если бы known_job_ids возвращал такие вакансии как «известные», они
навсегда выпадали бы из рассылки — поэтому и они ретраятся.

Настройки подписчика живут прямо в таблице subscribers:
- feeds  — JSON-список выбранных разделов (пустая строка = «по умолчанию»,
  то есть все разделы из HIRIFY_FEEDS);
- silent — 1 = тихий режим (без звука уведомлений);
- display — вид карточек: 'bubble' | 'rich' | 'classic' | '' (= auto).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

_LOCK = threading.RLock()

# Допустимые виды карточек (персональная настройка в /settings)
DISPLAY_MODES = ("auto", "bubble", "rich", "classic")
DEFAULT_DISPLAY = "auto"

# Статусы-«не финализировано»: монитор обязан повторить попытку.
# 'new' — добавлена в проход, который оборвался до mark_job (рестарт/сбой):
# такую вакансию нельзя считать обработанной, иначе она потеряется навсегда.
RETRYABLE_STATUSES = ("send_failed", "fetch_failed", "new")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      INTEGER PRIMARY KEY,
    slug        TEXT,
    url         TEXT,
    title       TEXT,
    company     TEXT,
    salary      TEXT,
    tg_contacts TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    sent_at     TEXT,
    status      TEXT NOT NULL DEFAULT 'new'  -- new|sent|skipped_no_contact|failed|fetch_failed|send_failed
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id     INTEGER PRIMARY KEY,
    username    TEXT,
    created_at  TEXT NOT NULL
);
-- настройки рассылки на подписчика добавляются миграцией в __init__
-- (feeds TEXT: JSON-массив ключей разделов, '' = по умолчанию; silent INTEGER)

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Тонкая обёртка над sqlite3, потокобезопасная (один замок)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            # WAL: безопаснее при сбоях и не блокирует читателей
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            # миграция старых баз: колонка attempts
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
            if "attempts" not in cols:
                self.conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            # миграция старых баз: настройки подписчика (feeds, silent, display)
            scols = {r[1] for r in self.conn.execute("PRAGMA table_info(subscribers)")}
            if "feeds" not in scols:
                self.conn.execute("ALTER TABLE subscribers ADD COLUMN feeds TEXT NOT NULL DEFAULT ''")
            if "silent" not in scols:
                self.conn.execute("ALTER TABLE subscribers ADD COLUMN silent INTEGER NOT NULL DEFAULT 0")
            if "display" not in scols:
                self.conn.execute("ALTER TABLE subscribers ADD COLUMN display TEXT NOT NULL DEFAULT ''")
            self.conn.commit()

    # ---------- вакансии ----------

    def known_job_ids(self, ids: Iterable[int]) -> set[int]:
        """Вернуть подмножество ids, которые уже обработаны (не требуют попытки).

        send_failed/fetch_failed/new в результат НЕ попадают — монитор их
        повторит ('new' — проход оборвался до финализации, напр. рестарт).
        Запрос идёт чанками: SQLite ограничивает число подставляемых переменных.
        """
        ids = list(ids)
        if not ids:
            return set()
        out: set[int] = set()
        retry_clause = (
            " AND status NOT IN ("
            + ",".join("?" * len(RETRYABLE_STATUSES))
            + ")"
        )
        with _LOCK:
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                ph = ",".join("?" * len(chunk))
                rows = self.conn.execute(
                    f"SELECT job_id FROM jobs WHERE job_id IN ({ph}){retry_clause}",
                    chunk + list(RETRYABLE_STATUSES),
                ).fetchall()
                out.update(r["job_id"] for r in rows)
        return out

    def job_attempts(self, job_id: int) -> int:
        with _LOCK:
            row = self.conn.execute("SELECT attempts FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return int(row["attempts"] or 0) if row else 0

    def bump_job_attempts(self, job_id: int) -> int:
        """+1 к счётчику попыток; возвращает новое значение."""
        with _LOCK:
            self.conn.execute(
                "UPDATE jobs SET attempts=COALESCE(attempts,0)+1 WHERE job_id=?", (job_id,)
            )
            self.conn.commit()
            row = self.conn.execute("SELECT attempts FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return int(row["attempts"] or 0) if row else 0

    def job_status(self, job_id: int) -> str | None:
        with _LOCK:
            row = self.conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row["status"] if row else None

    def get_job(self, job_id: int) -> dict | None:
        """Вся строка вакансии (slug/url/title/tg_contacts/…) — для кнопки
        «Показать контакты»: контакт материализуется в tg_contacts и кэшируется."""
        with _LOCK:
            row = self.conn.execute(
                "SELECT job_id, slug, url, title, company, salary, tg_contacts,"
                " attempts, first_seen, sent_at, status FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_job(
        self,
        job_id: int,
        slug: str,
        url: str,
        title: str = "",
        company: str = "",
        salary: str = "",
        tg_contacts: list | None = None,
    ) -> None:
        with _LOCK:
            self.conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (job_id, slug, url, title, company, salary, tg_contacts, first_seen, status)
                   VALUES (?,?,?,?,?,?,?,?, 'new')""",
                (
                    job_id,
                    slug,
                    url,
                    title,
                    company,
                    salary,
                    json.dumps(tg_contacts or [], ensure_ascii=False),
                    _now(),
                ),
            )
            self.conn.commit()

    def update_job_parsed(
        self,
        job_id: int,
        title: str = "",
        company: str = "",
        salary: str = "",
        tg_contacts: list | None = None,
    ) -> None:
        with _LOCK:
            self.conn.execute(
                """UPDATE jobs SET title=?, company=?, salary=?, tg_contacts=? WHERE job_id=?""",
                (
                    title,
                    company,
                    salary,
                    json.dumps(tg_contacts or [], ensure_ascii=False),
                    job_id,
                ),
            )
            self.conn.commit()

    def mark_job(self, job_id: int, status: str) -> None:
        with _LOCK:
            self.conn.execute(
                "UPDATE jobs SET status=?, sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE job_id=?",
                (status, status, _now(), job_id),
            )
            self.conn.commit()

    def prune_jobs(self, days: float, vacuum: bool = True) -> int:
        """Гигиена БД: удалить вакансии со first_seen старше days дней.

        Дедуп нужен только по свежим публикациям — фид отдаёт одни лишь
        последние страницы, вакансия недельной давимости в него уже не
        вернётся, поэтому старьё (включая зависшие ретраи send_failed/new)
        можно удалять безболезненно. Иначе таблица растёт вечно (~180 строк
        в сутки) и раздувает каждый коммит БД в git.

        vacuum=True — после удаления вернуть место файлу (VACUUM): коммит
        в репозиторий остаётся лёгким. Возвращает число удалённых строк.
        days<=0 — чистка выключена (0 удалений).
        """
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with _LOCK:
            cur = self.conn.execute("DELETE FROM jobs WHERE first_seen < ?", (cutoff,))
            removed = max(cur.rowcount or 0, 0)
            self.conn.commit()
            if removed and vacuum:
                try:
                    # В WAL-режиме VACUUM кладёт сжатую копию в -wal: без
                    # чекпоинта основной .db файл НЕ уменьшается (проверено:
                    # 774 КБ на диске при 6 страницах контента) — а именно
                    # его бот коммитит в git. Не в транзакции: после commit().
                    self.conn.execute("VACUUM")
                    self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:  # noqa: BLE001 — место вернём в следующий раз
                    pass
        return removed

    def stats(self) -> dict:
        with _LOCK:
            rows = self.conn.execute(
                """SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"""
            ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        sent_today = 0
        with _LOCK:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM jobs
                   WHERE sent_at >= date('now', 'start of day')"""
            ).fetchone()
            sent_today = row["n"]
        return {
            "total": sum(by_status.values()),
            "sent": by_status.get("sent", 0),
            "skipped_no_contact": by_status.get("skipped_no_contact", 0),
            "skipped_not_remote": by_status.get("skipped_not_remote", 0),
            "skipped_not_crypto": by_status.get("skipped_not_crypto", 0),
            "failed": by_status.get("failed", 0),
            "new": by_status.get("new", 0),
            "fetch_failed": by_status.get("fetch_failed", 0),
            "send_failed": by_status.get("send_failed", 0),
            "sent_today": sent_today,
            # «Не-рассылательные» группы для /stats: без контакта — отдельно,
            # всё отсеянное фильтрами (раздел/индустрия/скам/недоступные контакты) — в filtered.
            # РАНЬШЕ skipped сюда не попадал вовсе, а filtered не знал про
            # индустриальный фильтр — /stats занижал реальные цифры.
            "skipped": by_status.get("skipped_no_contact", 0),
            "filtered": (
                by_status.get("skipped_not_remote", 0)
                + by_status.get("skipped_not_crypto", 0)
                + by_status.get("skipped_off_industry", 0)
                + by_status.get("skipped_scam", 0)
                + by_status.get("skipped_restricted", 0)
                + by_status.get("skipped_not_vacancy", 0)  # посты ТГ-каналов не про вакансии
            ),
            # «Слишком старые»: опубликованы раньше границы свежести — не присылались
            "stale": by_status.get("skipped_stale", 0),
        }

    # ---------- подписчики ----------

    def add_subscriber(self, chat_id: int, username: str | None) -> None:
        with _LOCK:
            self.conn.execute(
                """INSERT OR IGNORE INTO subscribers (chat_id, username, created_at)
                   VALUES (?,?,?)""",
                (chat_id, username, _now()),
            )
            self.conn.commit()

    def remove_subscriber(self, chat_id: int) -> None:
        with _LOCK:
            self.conn.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
            self.conn.commit()

    def subscriber_ids(self) -> list[int]:
        with _LOCK:
            rows = self.conn.execute("SELECT chat_id FROM subscribers").fetchall()
        return [r["chat_id"] for r in rows]

    # ---------- настройки подписчика ----------

    @staticmethod
    def _parse_feeds(raw: str | None) -> set[str] | None:
        """'' → None («по умолчанию»), иначе множество ключей разделов."""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, list):
            return None
        return {str(x) for x in data if x}

    @staticmethod
    def _parse_display(raw: str | None) -> str:
        """'' / мусор → 'auto'; иначе допустимый вид карточек."""
        raw = (raw or "").strip().lower()
        return raw if raw in DISPLAY_MODES else DEFAULT_DISPLAY

    def subscriber(self, chat_id: int) -> dict | None:
        """Один подписчик: {chat_id, username, feeds: set|None, silent: bool, display: str}."""
        with _LOCK:
            row = self.conn.execute(
                "SELECT chat_id, username, feeds, silent, display FROM subscribers WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "chat_id": row["chat_id"],
            "username": row["username"],
            "feeds": self._parse_feeds(row["feeds"]),
            "silent": bool(row["silent"]),
            "display": self._parse_display(row["display"]),
        }

    def subscriber_settings(self) -> list[dict]:
        """Все подписчики с настройками:
        [{chat_id, feeds: set|None, silent: bool, display: str}]."""
        with _LOCK:
            rows = self.conn.execute(
                "SELECT chat_id, feeds, silent, display FROM subscribers"
            ).fetchall()
        return [
            {
                "chat_id": r["chat_id"],
                "feeds": self._parse_feeds(r["feeds"]),
                "silent": bool(r["silent"]),
                "display": self._parse_display(r["display"]),
            }
            for r in rows
        ]

    def update_subscriber_settings(
        self,
        chat_id: int,
        feeds: list[str] | None,
        silent: bool,
        display: str = DEFAULT_DISPLAY,
    ) -> None:
        """Сохранить настройки подписчика.

        feeds=None — сброс на «по умолчанию» (все разделы из HIRIFY_FEEDS);
        feeds=[] — ничего не присылать; иначе — выбранные ключи разделов.
        display — вид карточек: auto/bubble/rich/classic.
        Подписчика создаём при необходимости (чтобы настройки можно было
        менять и до /start).
        """
        display = self._parse_display(display)
        with _LOCK:
            self.conn.execute(
                "INSERT OR IGNORE INTO subscribers (chat_id, username, created_at) VALUES (?,NULL,?)",
                (chat_id, _now()),
            )
            raw = "" if feeds is None else json.dumps(sorted(feeds), ensure_ascii=False)
            self.conn.execute(
                "UPDATE subscribers SET feeds=?, silent=?, display=? WHERE chat_id=?",
                (raw, int(bool(silent)), display, chat_id),
            )
            self.conn.commit()

    # ---------- настройки ----------

    def get_setting(self, key: str, default: str = "") -> str:
        with _LOCK:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with _LOCK:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    def close(self) -> None:
        with _LOCK:
            try:
                # вливаем WAL в основной файл: после close состояние должно
                # целиком лежать в .db (важно для коммита БД в git воркфлоуом)
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()
