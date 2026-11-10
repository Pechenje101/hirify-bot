"""Тесты мониторинга: мгновенная отправка найденных вакансий, дедуп, адаптивный интервал."""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.monitor import Monitor  # noqa: E402
from app.parsing import Vacancy  # noqa: E402

FEED_HTML = """
<html><body>
  <a href="/jobs/101-senior-solidity-engineer">Solidity</a>
  <a href="/jobs/102-frontend-developer">Frontend</a>
  <a href="/jobs/103-no-contact-role">No contact</a>
</body></html>
"""

def job_html(title: str, username: str | None) -> str:
    contact = f'<div class="contacts"><a href="https://t.me/{username}">@{username}</a></div>' if username else ""
    return f"""<!DOCTYPE html><html><head><title>{title}</title>
<script type="application/ld+json">
{{"@type":"JobPosting","title":"{title}","hiringOrganization":{{"name":"Acme"}},
 "datePosted":"2026-11-10","jobLocationType":"TELECOMMUTE"}}
</script></head><body><h1>{title}</h1>{contact}</body></html>"""


JOB_PAGES = {
    "https://hirify.me/jobs/101-senior-solidity-engineer": job_html("Solidity Engineer", "acme_crypto_hr"),
    "https://hirify.me/jobs/102-frontend-developer": job_html("Frontend Developer", "acme_hr"),
    "https://hirify.me/jobs/103-no-contact-role": job_html("No Contact Role", None),
}


class FakeClient:
    """Вместо httpx: отдаём заготовленный HTML без сети."""

    def __init__(self, feed_html: str, job_pages: dict):
        self.feed_html = feed_html
        self.job_pages = job_pages
        self.feed_fetches = 0
        self.job_fetches = 0

    async def fetch_feed(self, feed, page: int = 1) -> str:
        self.feed_fetches += 1
        return self.feed_html if page == 1 else ""

    async def fetch_job(self, url: str) -> str:
        self.job_fetches += 1
        return self.job_pages[url]


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.silents: list[bool] = []
        self.displays: list[str] = []
        self.texts: list[str] = []

    async def send_vacancy(
        self, chat_id: int, v: Vacancy, *, silent: bool = False, display: str = "auto"
    ) -> str:
        self.sent.append((chat_id, v.title))
        self.silents.append(silent)
        self.displays.append(display)
        return "classic"

    async def send_text(self, chat_id: int, html: str) -> None:
        self.texts.append(html)


def make_monitor(monkeypatch_feeds: str = "remote") -> tuple[Monitor, FakeClient, FakeSender, Database]:
    cfg = Config(bot_token="x")
    cfg.hirify_feeds = monkeypatch_feeds
    cfg.remote_strict = False
    cfg.crypto_strict = False
    client = FakeClient(FEED_HTML, JOB_PAGES)
    sender = FakeSender()
    # mkdtemp не авто-удаляется: соединение sqlite живёт дольше функции
    tmpdir = tempfile.mkdtemp(prefix="hirify_test_")
    db = Database(Path(tmpdir) / "t.db")
    db.add_subscriber(1, "@tester")
    mon = Monitor(client, db, sender, cfg)
    return mon, client, sender, db


def test_poll_sends_each_vacancy_immediately_in_order():
    """Каждая подходящая вакансия уходит подписчику в том же проходе — без батчей."""
    mon, client, sender, db = make_monitor()
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["total"] == 3
    assert stats["sent"] == 2          # у 103 нет TG-контакта
    assert stats["skipped"] == 1
    # порядок — как в фиде, каждая отправлена сразу после парсинга
    assert sender.sent == [(1, "Solidity Engineer"), (1, "Frontend Developer")]
    assert db.job_status(101) == "sent"
    assert db.job_status(103) == "skipped_no_contact"
    db.close()


def test_second_poll_does_not_resend_known_jobs():
    mon, client, sender, db = make_monitor()
    asyncio.run(mon.poll_once(source="test"))
    sender.sent.clear()
    stats2 = asyncio.run(mon.poll_once(source="test"))
    assert stats2["total"] == 0 and stats2["sent"] == 0
    assert sender.sent == []
    db.close()


def test_next_interval_is_fast_after_findings():
    """Ключ к мгновенной доставке: было новое → следующий проход через fast_poll_sec."""
    mon, _, _, db = make_monitor()
    cfg = mon.cfg
    # пачка не влезла в лимит — догоняем быстро
    assert mon.next_interval({"total": cfg.max_per_poll, "sent": 3}) == cfg.fast_poll_sec
    # было движение (отправили/отфильтровали) — тоже быстро
    assert mon.next_interval({"total": 2, "sent": 1}) == cfg.fast_poll_sec
    assert mon.next_interval({"total": 1, "sent": 0}) == cfg.fast_poll_sec
    # пустой проход — ждём базовый интервал
    assert mon.next_interval({"total": 0, "sent": 0}) == cfg.poll_minutes * 60
    db.close()


def test_nudge_wakes_sleeping_loop():
    """nudge() прерывает сон цикла — проверка фида начинается немедленно."""
    mon, _, _, db = make_monitor()
    mon._last_poll_ts = 0.0  # кулдаун пройден

    async def scenario():
        # цикл «спит» 60 секунд, но nudge должен разбудить его почти сразу
        task = asyncio.create_task(mon._sleep(60))
        await asyncio.sleep(0.05)
        mon.nudge()
        start = time.monotonic()
        await asyncio.wait_for(task, timeout=5)
        return time.monotonic() - start

    elapsed = asyncio.run(scenario())
    assert elapsed < 5, f"nudge не разбудил сон: {elapsed:.1f}s"
    db.close()


def test_nudge_ignored_during_cooldown_and_busy():
    mon, _, _, db = make_monitor()
    mon._last_poll_ts = time.time()
    mon.nudge()  # только что был проход — кулдаун
    assert not mon._wake.is_set()
    # занят — тоже не будим
    mon._last_poll_ts = 0.0
    mon._busy = True
    mon.nudge()
    assert not mon._wake.is_set()
    mon._busy = False
    mon.nudge()  # теперь событие выставлено
    assert mon._wake.is_set()
    db.close()


def test_run_forever_stops_promptly():
    """stop() будит сон и цикл завершается, а не ждёт конца интервала."""
    mon, _, _, db = make_monitor()

    async def scenario():
        task = asyncio.create_task(mon.run_forever())
        await asyncio.sleep(0.2)
        mon.stop()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())  # не должно висеть
    db.close()


# ------------------------------------------------ настройки подписчика

def test_subscriber_feed_filter_excludes_other_sections():
    """Подписчик выбрал только «global»: вакансии из «remote» не должны приходить."""
    mon, client, sender, db = make_monitor()  # cfg feeds = "remote"
    db.update_subscriber_settings(1, feeds=["global"], silent=False)
    stats = asyncio.run(mon.poll_once(source="test"))
    # вакансии валидны и обработаны, но адресата в разделе remote нет:
    # 2 с контактом — no_audience, 1 без контакта — skipped
    assert stats["total"] == 3 and stats["no_audience"] == 2 and stats["sent"] == 0
    assert stats["skipped"] == 1
    assert sender.sent == []
    # финализированы без бесконечных ретраев
    assert db.job_status(101) == "sent"
    assert client.job_fetches == 3  # страницы всё равно открыли один раз
    # вернули «все разделы» — в новом проходе вакансии бы ушли, но они уже known
    db.update_subscriber_settings(1, feeds=None, silent=False)
    stats2 = asyncio.run(mon.poll_once(source="test"))
    assert stats2["total"] == 0
    db.close()


def test_silent_flag_is_passed_to_sender():
    """Тихий режим подписчика доходит до отправщика (disable_notification)."""
    mon, _, sender, db = make_monitor()
    db.update_subscriber_settings(1, feeds=None, silent=True)
    asyncio.run(mon.poll_once(source="test"))
    assert sender.silents == [True, True]
    db.close()


def test_display_mode_is_passed_to_sender():
    """Выбранный в /settings вид карточек доходит до отправщика."""
    mon, _, sender, db = make_monitor()
    db.update_subscriber_settings(1, feeds=None, silent=False, display="bubble")
    asyncio.run(mon.poll_once(source="test"))
    assert sender.displays == ["bubble", "bubble"]
    # дефолт — auto
    db.update_subscriber_settings(1, feeds=None, silent=False, display="")
    sender.displays.clear()
    asyncio.run(mon.poll_once(source="test"))
    assert sender.displays == []  # всё уже отправлено — новых нет
    db.close()


def test_send_retry_capped_at_max_attempts():
    """Залипшую доставку не ретраим вечно: после MAX_SEND_ATTEMPTS — failed."""
    from app.monitor import MAX_SEND_ATTEMPTS

    mon, _, _, db = make_monitor()

    class BrokenSender(FakeSender):
        async def send_vacancy(self, chat_id, v, *, silent=False, display="auto"):
            raise RuntimeError("bot blocked")

    mon.sender = BrokenSender()
    for _ in range(MAX_SEND_ATTEMPTS + 1):
        asyncio.run(mon.poll_once(source="test"))
    # обе вакансии с контактом сняты с ретрая; попыток — ровно потолок
    assert db.job_status(101) == "failed"
    assert db.job_status(102) == "failed"
    assert db.job_attempts(101) == MAX_SEND_ATTEMPTS
    # снятые с ретрая больше не поднимаются
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["total"] == 0 and stats["failed"] == 0
    db.close()


# ---------------------------------------------------- ретраи доставки/загрузки

def test_failed_send_is_retried_next_poll():
    """Если ни одна отправка не прошла — вакансия не «сгорает», а ретраится."""
    mon, client, sender, db = make_monitor()

    class BrokenSender(FakeSender):
        async def send_vacancy(self, chat_id, v, *, silent=False, display="auto"):
            raise RuntimeError("bot blocked")

    mon.sender = BrokenSender()
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["send_failed"] == 2
    assert stats["sent"] == 0
    assert db.job_status(101) == "send_failed"

    # починили отправку — вакансии должны уйти повторно в следующем проходе
    mon.sender = sender
    stats2 = asyncio.run(mon.poll_once(source="test"))
    assert stats2["sent"] == 2
    assert db.job_status(101) == "sent"
    db.close()


def test_fetch_failure_is_retried_up_to_limit():
    """Сетевой сбой загрузки страницы: до MAX_FETCH_ATTEMPTS попыток, потом failed."""
    from app.hirify import HirifyError
    from app.monitor import MAX_FETCH_ATTEMPTS

    class FlakyClient(FakeClient):
        async def fetch_job(self, url):
            if "101" in url:
                raise HirifyError("connection timeout")
            return await super().fetch_job(url)

    mon, _, sender, db = make_monitor()
    mon.client = FlakyClient(FEED_HTML, JOB_PAGES)
    for _ in range(MAX_FETCH_ATTEMPTS):
        stats = asyncio.run(mon.poll_once(source="test"))
    assert db.job_status(101) == "failed"
    assert stats["failed"] == 1 and stats["fetch_failed"] == 0
    # после failed — больше не пытается (known)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["total"] == 0
    # а вторая вакансия была отправлена ещё в первом проходе
    assert any(t == "Frontend Developer" for _, t in sender.sent)
    db.close()


def test_http_404_fails_immediately_without_retries():
    """404/410 — вакансия удалена: помечаем failed сразу, без бессмысленных ретраев."""
    from app.hirify import HirifyNotFound

    class GoneClient(FakeClient):
        async def fetch_job(self, url):
            if "101" in url:
                raise HirifyNotFound("HTTP 404")
            return await super().fetch_job(url)

    mon, _, _, db = make_monitor()
    mon.client = GoneClient(FEED_HTML, JOB_PAGES)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["failed"] == 1 and stats["fetch_failed"] == 0
    assert db.job_status(101) == "failed"
    assert db.job_attempts(101) == 0  # счётчик попыток не тратится
    db.close()


def test_no_subscribers_no_job_fetching():
    """Без подписчиков страницы вакансий не открываем (экономим запросы к сайту)."""
    mon, client, _, db = make_monitor()
    db.remove_subscriber(1)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["total"] == 0
    assert client.job_fetches == 0
    # статус не финализирован — когда появится подписчик, вакансии придут
    db.add_subscriber(2, "@late")
    stats2 = asyncio.run(mon.poll_once(source="test"))
    assert stats2["sent"] == 2
    db.close()
