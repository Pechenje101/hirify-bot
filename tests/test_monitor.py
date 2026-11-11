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

FEED_ITEMS = [
    {"id": 101, "slug": "101-senior-solidity-engineer", "source": "telegram"},
    {"id": 102, "slug": "102-frontend-developer", "source": "telegram"},
    {"id": 103, "slug": "103-no-contact-role", "source": "telegram"},
]


def vacancy_json(title: str, job_id: int) -> dict:
    """Сырой JSON GET /api/vacancies/{slug} — как отдаёт api.hirify.me."""
    return {
        "id": str(job_id),
        "slug": f"{job_id}-{title.lower().replace(' ', '-')}",
        "title": title,
        "original_title": title,
        "company_title": "Acme",
        "text": f"<strong>{title}</strong><br/>Мы ищем {title.lower()}. Пишите!",
        "tldr": "кратко",
        "work_format": ["remote"],
        "remote_type": "global",
        "work_type": "fulltime",
        "employee_type": ["employment"],
        "salary": None,
        "tags": [{"id": 1, "name": "python"}],
        "regions": [{"id": 1, "code": "russia", "name": "Россия"}],
        "grades": [],
        "created_at": "2026-11-10T13:01:11.000000Z",
        "source": "telegram",
        "is_scam": False,
        "is_potential_scam": False,
    }


def contacts_json(username: str | None) -> dict:
    """Ответ POST /api/vacancies/{slug}/contacts."""
    items = [] if not username else [{
        "type": "telegram",
        "value": f"https://t.me/{username}",
        "short_code": "abc12",
    }]
    return {"vacancy_id": 1, "contacts": items, "access_type": "free_vacancy"}


VACANCIES = {
    "101-senior-solidity-engineer": (vacancy_json("Solidity Engineer", 101),
                                     contacts_json("acme_crypto_hr")),
    "102-frontend-developer": (vacancy_json("Frontend Developer", 102),
                               contacts_json("acme_hr")),
    "103-no-contact-role": (vacancy_json("No Contact Role", 103), contacts_json(None)),
}


class FakeClient:
    """Вместо httpx: отдаём заготовленные JSON без сети."""

    def __init__(self, feed_items: list, api: dict):
        self.feed_items = feed_items
        self.api = api
        self.feed_fetches = 0
        self.job_fetches = 0

    async def fetch_feed_api(self, feed, page: int = 1) -> list:
        self.feed_fetches += 1
        return self.feed_items if page == 1 else []

    async def fetch_vacancy(self, slug: str) -> dict:
        self.job_fetches += 1
        return self.api[slug][0]

    async def fetch_contacts(self, slug: str) -> dict:
        return self.api[slug][1]


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
    cfg.industry_strict = False  # по умолчанию включён; в базовых тестах выключен
    client = FakeClient(FEED_ITEMS, VACANCIES)
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
    # адресатов раздела remote нет — все три обработаны без доставки и
    # БЕЗ запроса контактов (просмотры контактов сайт считает — не жжём их):
    # фильтры решаются по данным вакансии, а адресатов проверяем до контактов
    assert stats["total"] == 3 and stats["no_audience"] == 3 and stats["sent"] == 0
    assert stats["skipped"] == 0
    assert sender.sent == []
    # финализированы без бесконечных ретраев
    assert db.job_status(101) == "sent"
    assert client.job_fetches == 3  # данные вакансии по каждой — один раз
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
    """Сетевой сбой загрузки данных: до MAX_FETCH_ATTEMPTS попыток, потом failed."""
    from app.hirify import HirifyError
    from app.monitor import MAX_FETCH_ATTEMPTS

    class FlakyClient(FakeClient):
        async def fetch_vacancy(self, slug):
            if slug.startswith("101"):
                raise HirifyError("connection timeout")
            return await super().fetch_vacancy(slug)

    mon, _, sender, db = make_monitor()
    mon.client = FlakyClient(FEED_ITEMS, VACANCIES)
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
        async def fetch_vacancy(self, slug):
            if slug.startswith("101"):
                raise HirifyNotFound("HTTP 404")
            return await super().fetch_vacancy(slug)

    mon, _, _, db = make_monitor()
    mon.client = GoneClient(FEED_ITEMS, VACANCIES)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["failed"] == 1 and stats["fetch_failed"] == 0
    assert db.job_status(101) == "failed"
    assert db.job_attempts(101) == 0  # счётчик попыток не тратится
    db.close()


def test_expired_session_warns_and_retries_later():
    """401 от API: вакансия остаётся на ретрай, подписчику уходит подсказка."""
    from app.hirify import HirifyAuthError

    class AuthlessClient(FakeClient):
        async def fetch_vacancy(self, slug):
            if slug.startswith("101"):
                raise HirifyAuthError("сессия не принята (HTTP 401)")
            return await super().fetch_vacancy(slug)

    mon, _, sender, db = make_monitor()
    mon.client = AuthlessClient(FEED_ITEMS, VACANCIES)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["fetch_failed"] == 1 and stats["sent"] == 1
    assert db.job_status(101) == "fetch_failed"  # ретрай, когда cookie оживёт
    assert sender.texts  # подсказка про cookie отправлена админу
    db.close()


def test_scam_vacancies_are_skipped():
    """Помеченные сайтом как скам не рассылаем."""
    scam = vacancy_json("Miracle CTO Fund", 104)
    scam["is_scam"] = True
    api = dict(VACANCIES)
    api["104-miracle-cto-fund"] = (scam, contacts_json("scam_hr"))
    feed = FEED_ITEMS + [{"id": 104, "slug": "104-miracle-cto-fund", "source": "telegram"}]
    mon, _, sender, db = make_monitor()
    mon.client = FakeClient(feed, api)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["filtered"] == 1
    assert all(t != "Miracle CTO Fund" for _, t in sender.sent)
    db.close()


def test_off_industry_vacancies_are_filtered():
    """INDUSTRY_STRICT: уходит только целевая индустрия (крипта/iGaming/...),
    вакансии «обычных» компаний отфильтровываются без ретраев."""
    igaming = vacancy_json("Sportsbook Backend Developer", 104)
    api = dict(VACANCIES)
    api["104-sportsbook-backend-developer"] = (igaming, contacts_json("igaming_hr"))
    feed = FEED_ITEMS + [{"id": 104, "slug": "104-sportsbook-backend-developer", "source": "telegram"}]
    mon, _, sender, db = make_monitor()
    mon.cfg.industry_strict = True
    mon.client = FakeClient(feed, api)
    stats = asyncio.run(mon.poll_once(source="test"))
    # 101 (крипта) и 104 (спортсбук) — целевые; 102 (фронт) и 103 (без индустрии
    # и без контакта — фильтры теперь до запроса контактов) — отфильтрованы
    assert stats["filtered"] == 2
    assert sender.sent == [(1, "Solidity Engineer"), (1, "Sportsbook Backend Developer")]
    assert db.job_status(102) == "skipped_off_industry"
    assert db.job_status(103) == "skipped_off_industry"
    assert db.job_status(104) == "sent"
    db.close()


def test_premium_vacancies_finalized_without_retry():
    """403 «премиум/лимит попыток» — финально: без ретраев и рассылки."""
    from app.hirify import HirifyRestrictedError

    class PremiumClient(FakeClient):
        async def fetch_contacts(self, slug):
            if slug.startswith("101"):
                raise HirifyRestrictedError("доступ ограничен (HTTP 403)")
            return await super().fetch_contacts(slug)

    mon, _, _, db = make_monitor()
    premium = PremiumClient(FEED_ITEMS, VACANCIES)
    mon.client = premium
    for _ in range(2):
        stats = asyncio.run(mon.poll_once(source="test"))
    assert db.job_status(101) == "skipped_restricted"
    assert db.job_attempts(101) == 0
    # первый проход открыл все три вакансии, второй — ни одной (все финальны)
    assert premium.job_fetches == 3
    db.close()


def test_channel_post_contact_counts():
    """Контакт-пост в канале — тоже прямой контакт: вакансия уходит."""
    chan = dict(VACANCIES)
    chan["104-channel-role"] = (
        vacancy_json("Channel Only Role", 104),
        {"vacancy_id": 104, "contacts": [
            {"type": "telegram", "value": "https://t.me/iGaming_work/5195", "short_code": "1eHg0"}
        ], "access_type": "free_vacancy"},
    )
    feed = FEED_ITEMS + [{"id": 104, "slug": "104-channel-role", "source": "telegram"}]
    mon, _, sender, db = make_monitor()
    mon.client = FakeClient(feed, chan)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["sent"] == 3
    db.close()


def test_no_subscribers_no_job_fetching():
    """Без подписчиков API вакансий не дёргаем (экономим запросы к сайту)."""
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


def test_automation_flag_stops_poll_and_retries_later():
    """403 automation_detected — временный: проход останавливается, вакансии
    остаются на ретрай (не сгорают в skipped_restricted)."""
    from app.hirify import HirifyAutomationError

    class FlaggedClient(FakeClient):
        def __init__(self, *a):
            super().__init__(*a)
            self.flagged = True

        async def fetch_contacts(self, slug):
            if self.flagged:
                raise HirifyAutomationError("сайт заподозрил автоматизацию (HTTP 403)")
            return await super().fetch_contacts(slug)

    mon, _, sender, db = make_monitor()
    flagged = FlaggedClient(FEED_ITEMS, VACANCIES)
    mon.client = flagged
    stats = asyncio.run(mon.poll_once(source="test"))
    # первая же вакансия словила флаг — обработка остановлена, ничего не отправлено
    assert stats["fetch_failed"] == 1 and stats["sent"] == 0
    assert db.job_status(101) == "fetch_failed"   # ретрай позже
    assert sender.sent == []
    # флаг спал — следующий проход доставляет всё
    flagged.flagged = False
    mon._automation_until = 0.0  # пауза закончилась
    stats2 = asyncio.run(mon.poll_once(source="test"))
    assert stats2["sent"] == 2
    assert db.job_status(101) == "sent"
    db.close()


def test_automation_backoff_escalates_and_survives_restart():
    """Пауза автоматизации живёт в БД (переживает замещение ранов) и растёт
    с каждым повторным срабатыванием — не кормим флаг постоянными попытками."""
    from app.hirify import HirifyAutomationError
    from app.monitor import AUTOMATION_BACKOFF_MIN

    class FlaggedClient(FakeClient):
        async def fetch_contacts(self, slug):
            raise HirifyAutomationError("сайт заподозрил автоматизацию (HTTP 403)")

    mon, _, _, db = make_monitor()
    mon.client = FlaggedClient(FEED_ITEMS, VACANCIES)
    asyncio.run(mon.poll_once(source="test"))
    first = mon._automation_until
    assert 89 * 60 < first - time.time() <= 90 * 60  # первая ступень — 90 мин
    assert db.get_setting("automation_flags", "") == "1"
    assert db.get_setting("automation_until", "") == str(int(first))

    # «ран заместился»: новый Monitor над той же БД подхватывает паузу из settings
    mon2 = Monitor(mon.client, db, mon.sender, mon.cfg)
    assert mon2._automation_until == int(first)
    assert mon2._automation_flag_count == 1

    # второе срабатывание — ступень выше (240 мин), счётчик в БД растёт
    mon2._automation_until = 0.0  # имитируем истечение паузы
    asyncio.run(mon2.poll_once(source="test"))
    second = mon2._automation_until
    assert 239 * 60 < second - time.time() <= 240 * 60
    assert db.get_setting("automation_flags", "") == "2"

    # успешный полный просмотр — счётчик и пауза сбрасываются (флаг снят)
    mon2._automation_flag_count = 1
    mon2._automation_until = 0.0  # пауза истекла — пробуем, сайт уже здоров
    healed = FakeClient(FEED_ITEMS, VACANCIES)
    mon2.client = healed
    stats = asyncio.run(mon2.poll_once(source="test"))
    assert stats["sent"] == 2
    assert mon2._automation_flag_count == 0 and mon2._automation_until == 0.0
    assert db.get_setting("automation_flags", "") == "0"
    db.close()


def test_off_industry_job_never_requests_contacts():
    """Индустриальный фильтр решается по данным вакансии: «дорогой» POST
    контактов делаем только для целевых вакансий."""

    class SpyClient(FakeClient):
        def __init__(self, *a):
            super().__init__(*a)
            self.contact_fetches: list[str] = []

        async def fetch_contacts(self, slug):
            self.contact_fetches.append(slug)
            return await super().fetch_contacts(slug)

    mon, _, _, db = make_monitor()
    mon.cfg.industry_strict = True  # боевое значение
    spy = SpyClient(FEED_ITEMS, VACANCIES)
    mon.client = spy
    stats = asyncio.run(mon.poll_once(source="test"))
    # 101 (Solidity — крипта) — целевая; 102 (фронтенд) и 103 — мимо индустрии
    assert stats["filtered"] == 2 and stats["sent"] == 1
    assert spy.contact_fetches == ["101-senior-solidity-engineer"]
    assert db.job_status(102) == "skipped_off_industry"
    db.close()
