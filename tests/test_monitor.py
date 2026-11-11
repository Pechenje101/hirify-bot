"""Тесты мониторинга: мгновенная отправка найденных вакансий, дедуп, адаптивный интервал."""
import asyncio
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.channels import channel_job_id  # noqa: E402
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
        self.landing_fetches = 0
        self.landing_html: dict[str, str] = {}   # slug → HTML публичной страницы

    async def fetch_feed_api(self, feed, page: int = 1) -> list:
        self.feed_fetches += 1
        return self.feed_items if page == 1 else []

    async def fetch_vacancy(self, slug: str) -> dict:
        self.job_fetches += 1
        return self.api[slug][0]

    async def fetch_contacts(self, slug: str) -> dict:
        return self.api[slug][1]

    async def fetch_job(self, url: str) -> str:
        """Публичная страница вакансии (анонимный GET лендинга).

        По умолчанию — пустая страница (контакта нет) → бот уходит в
        auth-путь, как раньше. Тесты кладут HTML с JSON-LD в landing_html."""
        self.landing_fetches += 1
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return self.landing_html.get(slug, "")


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
    cfg.tg_channels = ""  # в базовых тестах каналов нет — сеть не трогаем
    cfg.remote_strict = False
    cfg.crypto_strict = False
    cfg.industry_strict = False  # по умолчанию включён; в базовых тестах выключен
    # квоты аккаунта не должны мешать базовым тестам (много проходов подряд)
    cfg.auth_views_hour_max = 10**9
    cfg.auth_views_day_max = 10**9
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
    # БЕЗ единого запроса (ни API вакансий, ни контактов): адресаты
    # проверяются ДО открытия — просмотры аккаунта не тратятся вовсе
    assert stats["total"] == 3 and stats["no_audience"] == 3 and stats["sent"] == 0
    assert stats["skipped"] == 0
    assert sender.sent == []
    # финализированы без бесконечных ретраев
    assert db.job_status(101) == "sent"
    assert client.job_fetches == 0  # ни одна вакансия не открывалась
    assert client.landing_fetches == 0  # и анонимный лендинг тоже не нужен
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

    # успешный полный просмотр: пауза снимается, но недавний флаг ПОМНИТСЯ —
    # счётчик эскалации держится 48 ч (повторный флаг сразу бьёт ступенью выше)
    mon2._automation_flag_count = 1
    mon2._automation_until = 0.0  # пауза истекла — пробуем, сайт уже здоров
    healed = FakeClient(FEED_ITEMS, VACANCIES)
    mon2.client = healed
    stats = asyncio.run(mon2.poll_once(source="test"))
    assert stats["sent"] == 2
    assert mon2._automation_until == 0.0
    assert mon2._automation_flag_count == 1  # память эскалации: сброс только через 48 ч
    assert db.get_setting("automation_flags", "") == "2"  # в БД счётчик не тронут

    # а вот 48+ часов без флагов — счётчик сбрасывается полностью
    mon2._automation_last_flag = time.time() - 49 * 3600
    mon2._automation_healed([{"chat_id": 1}])
    assert mon2._automation_flag_count == 0
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


# --------------------------------------------------- ритм опроса (анти-флаг)

def test_poll_wait_has_human_jitter():
    """Пустой проход → интервал базовый, но НЕ ровный: гуляет ×0.8–1.35,
    минимум 60 с — метроном «раз в 5.00 минут» выдаёт автоматизацию."""
    from app.monitor import POLL_JITTER

    mon, _, _, db = make_monitor()
    base = mon.cfg.poll_minutes * 60
    waits = {round(mon._post_poll_wait({"total": 0, "sent": 0})) for _ in range(60)}
    assert all(base * POLL_JITTER[0] - 1 <= w <= base * POLL_JITTER[1] + 1 for w in waits)
    assert len(waits) > 5, "интервал не гуляет — метроном остался"
    # было движение — быстрый повтор тоже с джиттером, но не короче минуты
    fast = {round(mon._post_poll_wait({"total": 1, "sent": 1})) for _ in range(40)}
    fbase = mon.cfg.fast_poll_sec
    assert all(fbase * POLL_JITTER[0] - 1 <= w <= fbase * POLL_JITTER[1] + 1 for w in fast)
    db.close()


def test_poll_wait_slow_and_wake_at_pause_end():
    """Пока сайт держит флаг автоматизации: фиды дёргаем редко (раз в 30 мин),
    но к концу паузы просыпаемся точно (±секунды) — сразу добрать бэклог."""
    from app.monitor import AUTOMATION_POLL_SEC, POLL_JITTER

    mon, _, _, db = make_monitor()
    # флаг свежий: паузы ещё полтора часа → спим максимум 30 мин
    mon._automation_until = time.time() + 90 * 60
    w = mon._post_poll_wait({"automation_pause": True, "total": 0, "sent": 0})
    assert AUTOMATION_POLL_SEC - 1 <= w <= AUTOMATION_POLL_SEC  # min(1800, ~5430)
    # пауза вот-вот кончится → будим почти сразу, не спим полчаса
    mon._automation_until = time.time() + 40
    w2 = mon._post_poll_wait({"automation_pause": True, "total": 0, "sent": 0})
    assert 40 <= w2 <= 70
    # флага нет — обычный джиттер-режим (не 30 минут!)
    mon._automation_until = 0.0
    w3 = mon._post_poll_wait({"total": 0, "sent": 0})
    assert w3 <= mon.cfg.poll_minutes * 60 * POLL_JITTER[1]
    db.close()


# --------------------------------------- флаг автоматизации на запросе ФИДА

def test_feed_automation_flag_sets_pause():
    """403 automation_detected на ГЕТ-е фида — редкий, но реальный случай:
    без обработки монитор продолжал бы дёргать закрытый API каждые 5 минут.
    Теперь флаг ставит паузу с эскалацией (как для вакансий) и стопает проход."""
    from app.hirify import HirifyAutomationError

    class FeedFlaggedClient(FakeClient):
        async def fetch_feed_api(self, feed, page: int = 1) -> list:
            raise HirifyAutomationError("сайт заподозрил автоматизацию (HTTP 403)")

    mon, _, _, db = make_monitor()
    mon.client = FeedFlaggedClient(FEED_ITEMS, VACANCIES)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["automation_pause"] is True
    assert stats["total"] == 0  # ни одной вакансии не тронули
    assert mon._automation_flag_count == 1
    assert db.get_setting("automation_flags", "") == "1"
    assert mon._automation_until > time.time()  # пауза стоит
    db.close()


# ---------------------------------------------------- «метла» после паузы

def test_catchup_sweep_after_pause_expires():
    """Пауза истекла → первый проход идёт ГЛУБОЖЕ обычного (catchup_feed_pages):
    за часы ограничения вакансии накопились, и хвост мог уйти со страницы 1
    навсегда. Следующий проход — уже обычная глубина."""
    class DeepFeedClient(FakeClient):
        """Каждая страница фида «полная» (15 вакансий) — идём до глубины прохода."""

        def __init__(self, items):
            super().__init__(items, VACANCIES)
            self.pages_fetched = 0

        async def fetch_feed_api(self, feed, page: int = 1) -> list:
            self.pages_fetched += 1
            return self.feed_items

    items = [{"id": 200 + i, "slug": f"{200 + i}-job", "source": "telegram"}
             for i in range(15)]
    mon, client, sender, db = make_monitor()  # один раздел (remote)
    client = DeepFeedClient(items)
    mon.client = client
    mon.cfg.catchup_feed_pages = 4
    # все вакансии уже известны — «метла» проверяет только фид, без API-запросов
    for it in items:
        db.add_job(it["id"], it["slug"], "https://hirify.me/jobs/x")
        db.mark_job(it["id"], "sent")

    mon._automation_until = time.time() - 10  # пауза только что истекла
    stats = asyncio.run(mon.poll_once(source="test"))
    assert client.pages_fetched == 4          # прошли 4 страницы, а не одну
    assert mon._automation_until == 0.0       # пауза зачищена
    assert db.get_setting("automation_until", "") == "0"

    # следующий проход — обычная глубина (страницы feed_pages, а не 4)
    before = client.pages_fetched
    stats2 = asyncio.run(mon.poll_once(source="test"))
    assert client.pages_fetched == before + 1
    assert stats["total"] == 0 and stats2["total"] == 0
    db.close()


# ------------------------------------------- суточный ритм: ночь и утро

def test_night_hours_slowdown(monkeypatch):
    """Ночь (01:00–06:59 МСК): «тихие» проходы втрое длиннее, но по-прежнему
    с джиттером; быстрые повторы после находок не замедляются — хвост пачки
    добираем сразу; днём замедления нет, границы часа — по МСК."""
    from app.monitor import NIGHT_SLOWDOWN, POLL_JITTER

    mon, _, _, db = make_monitor()
    base = mon.cfg.poll_minutes * 60
    fast = mon.cfg.fast_poll_sec

    # 03:00 МСК — спим
    monkeypatch.setattr("app.monitor._msk_hour", lambda ts=None: 3)
    assert mon._night_slowdown() == NIGHT_SLOWDOWN
    waits = {round(mon._post_poll_wait({"total": 0, "sent": 0})) for _ in range(40)}
    lo, hi = base * NIGHT_SLOWDOWN * POLL_JITTER[0], base * NIGHT_SLOWDOWN * POLL_JITTER[1]
    assert all(lo - 1 <= w <= hi + 1 for w in waits)
    assert len(waits) > 5, "ночные интервалы не гуляют — метроном вернулся"
    # было движение — быстрый повтор БЕЗ ночного замедления
    w_fast = mon._post_poll_wait({"total": 1, "sent": 1})
    assert fast * POLL_JITTER[0] - 1 <= w_fast <= fast * POLL_JITTER[1] + 1

    # 12:00 МСК — день, замедления нет
    monkeypatch.setattr("app.monitor._msk_hour", lambda ts=None: 12)
    assert mon._night_slowdown() == 1.0
    w_day = mon._post_poll_wait({"total": 0, "sent": 0})
    assert w_day <= base * POLL_JITTER[1]

    # границы: 00:59 ещё день, 07:00 уже утро
    monkeypatch.setattr("app.monitor._msk_hour", lambda ts=None: 0)
    assert mon._night_slowdown() == 1.0
    monkeypatch.setattr("app.monitor._msk_hour", lambda ts=None: 7)
    assert mon._night_slowdown() == 1.0
    db.close()


def test_morning_deep_sweep_once_per_day(monkeypatch):
    """Первый проход после ночи (час ≥ 7 МСК) — «метла» глубиной
    catchup_feed_pages, ОДИН раз в день (метка даты в БД — переживает
    замещение ранов); ночью «метла» не включается — бэклог ждёт утра."""
    class DeepFeedClient(FakeClient):
        def __init__(self, items):
            super().__init__(items, VACANCIES)
            self.pages_fetched = 0

        async def fetch_feed_api(self, feed, page: int = 1) -> list:
            self.pages_fetched += 1
            return self.feed_items

    items = [{"id": 300 + i, "slug": f"{300 + i}-job", "source": "telegram"}
             for i in range(15)]
    monkeypatch.setattr("app.monitor._msk_hour", lambda ts=None: 9)  # утро
    mon, _, sender, db = make_monitor()
    client = DeepFeedClient(items)
    mon.client = client
    mon.cfg.catchup_feed_pages = 4
    for it in items:  # все уже известны — «метла» только листает фид
        db.add_job(it["id"], it["slug"], "https://hirify.me/jobs/x")
        db.mark_job(it["id"], "sent")

    stats = asyncio.run(mon.poll_once(source="test"))
    assert client.pages_fetched == 4          # утренняя «метла»: 4 страницы
    assert db.get_setting("deep_sweep_date", "") == mon._today_msk()

    before = client.pages_fetched
    stats2 = asyncio.run(mon.poll_once(source="test"))
    assert client.pages_fetched == before + 1  # второй проход — обычная глубина

    # ночью даже без метки «метла» не включается — ждём утра
    monkeypatch.setattr("app.monitor._msk_hour", lambda ts=None: 3)
    db.set_setting("deep_sweep_date", "")
    stats3 = asyncio.run(mon.poll_once(source="test"))
    assert client.pages_fetched == before + 2  # +1 страница, а не 4
    assert stats["total"] == 0 and stats2["total"] == 0 and stats3["total"] == 0
    db.close()


# ---------------------------------------------------------------- префильтр фида

def test_prefilter_skips_off_industry_without_opening():
    """Вакансию «мимо индустрии» видно уже по элементу фида (title+tldr):
    не открываем вовсе — минус один «просмотр» аккаунта."""
    items = [
        {"id": 201, "slug": "201-sales-manager-horeca", "source": "telegram",
         "title": "Менеджер по продажам (HoReCa)",
         "tldr": "Опыт продаж в HoReCa от 3 лет, ведение ключевых клиентов",
         "work_format": ["remote"], "company_title": "Ресторан"},
        {"id": 202, "slug": "202-media-buyer-igaming", "source": "telegram",
         "title": "Media Buyer (iGaming)",
         "tldr": "Арбитраж трафика на betting-офферы, Facebook",
         "work_format": ["remote"], "company_title": "NDA"},
    ]
    api = {"202-media-buyer-igaming": (vacancy_json("Media Buyer (iGaming)", 202),
                                        contacts_json("igaming_hr"))}

    mon, _, _, db = make_monitor()
    mon.cfg.industry_strict = True          # боевое значение
    spy = FakeClient(items, api)
    mon.client = spy

    stats = asyncio.run(mon.poll_once(source="test"))
    assert spy.job_fetches == 1           # HoReCa не открывали вообще
    assert db.job_status(201) == "skipped_off_industry"
    assert stats["filtered"] == 1 and stats["sent"] == 1
    db.close()


def test_prefilter_scam_flag_skips_opening():
    """Флаг скама приходит прямо в элементе фида — тоже не открываем."""
    items = [{"id": 203, "slug": "203-trader", "source": "telegram",
              "title": "Трейдер-аналитик", "tldr": "лёгкие деньги",
              "is_scam": True, "work_format": ["remote"]}]
    mon, _, _, db = make_monitor()
    spy = FakeClient(items, {})
    mon.client = spy
    stats = asyncio.run(mon.poll_once(source="test"))
    assert spy.job_fetches == 0
    assert db.job_status(203) == "skipped_scam"
    assert stats["filtered"] == 1
    db.close()


def test_prefilter_disabled_opens_everything():
    """FEED_PREFILTER=0 — прежнее поведение: открываем и фильтруем по полным данным."""
    items = [{"id": 204, "slug": "204-horeca", "source": "telegram",
              "title": "Менеджер (HoReCa)", "tldr": "продажи в HoReCa",
              "work_format": ["remote"]}]
    api = {"204-horeca": (vacancy_json("Менеджер HoReCa", 204), contacts_json("hr"))}
    mon, _, _, db = make_monitor()
    mon.cfg.industry_strict = True
    mon.cfg.feed_prefilter = False
    spy = FakeClient(items, api)
    mon.client = spy
    stats = asyncio.run(mon.poll_once(source="test"))
    assert spy.job_fetches == 1           # открыли, чтобы понять: мимо индустрии
    assert db.job_status(204) == "skipped_off_industry"
    db.close()


def test_prefilter_item_without_text_still_opens():
    """Элемент без title/tldr не судим — открываем, как раньше."""
    items = [{"id": 205, "slug": "205-plain", "source": "telegram"}]
    api = {"205-plain": (vacancy_json("Solidity Engineer", 205), contacts_json("hr"))}
    mon, _, _, db = make_monitor()
    mon.cfg.industry_strict = True
    spy = FakeClient(items, api)
    mon.client = spy
    stats = asyncio.run(mon.poll_once(source="test"))
    assert spy.job_fetches == 1           # нечего фильтровать — открыли
    assert stats["sent"] == 1                # полный фильтр пропустил (крипта)
    db.close()


def test_prefilter_non_remote_work_format():
    """Офисный формат виден в work_format элемента фида — не открываем."""
    items = [{"id": 206, "slug": "206-office-igaming", "source": "telegram",
              "title": "Account Manager (iGaming)", "tldr": "betting, casino",
              "work_format": ["office"]}]
    mon, _, _, db = make_monitor()
    mon.cfg.remote_strict = True             # раздел remote — только удалёнка
    mon.cfg.industry_strict = True
    spy = FakeClient(items, {})
    mon.client = spy
    asyncio.run(mon.poll_once(source="test"))
    assert spy.job_fetches == 0
    assert db.job_status(206) == "skipped_not_remote"
    db.close()


# ---------------------------------------------------------------- квоты аккаунта

def test_auth_hour_quota_defers_rest_of_batch():
    """Часовой лимит запросов от имени аккаунта: остаток пачки не теряется —
    вакансии остаются невидимыми и подбираются следующим проходом."""
    mon, _, _, db = make_monitor()
    mon.cfg.auth_views_hour_max = 1          # хватит ровно на одну вакансию

    stats = asyncio.run(mon.poll_once(source="test"))
    # 101-я открыта и отправлена; 102 и 103 отложены (102: 2 «просмотра»
    # — данные+контакты — уже потрачены). Анонимный лендинг пуст → auth-путь;
    # строки добавлены как 'new' и вернутся ретраем
    assert stats["sent"] == 1 and stats["quota_deferred"] >= 1
    assert db.job_status(101) == "sent"
    assert db.job_status(102) in (None, "new") and db.job_status(103) in (None, "new")

    mon._auth_hour_ts.clear()                # «прошёл час» — окно скользнуло
    stats2 = asyncio.run(mon.poll_once(source="test"))
    assert stats2["sent"] == 1               # 102-я догнана (103 — снова квота)
    mon._auth_hour_ts.clear()
    stats3 = asyncio.run(mon.poll_once(source="test"))
    assert stats3["skipped"] == 1            # 103-я без TG-контакта — отсеяна
    db.close()


def test_auth_day_quota_persists_in_db():
    """Суточный счётчик живёт в БД — замещение ранов не «обнуляет» лимит."""
    mon, _, _, db = make_monitor()
    mon.cfg.auth_views_day_max = 1
    asyncio.run(mon.poll_once(source="test"))
    assert db.get_setting("auth_views_count", "") == "2"   # 101: данные+контакты
    assert db.job_status(102) in (None, "new")              # квота: только add_job, auth нет

    # «новый ран»: Monitor над той же БД видит потраченную квоту
    mon2 = Monitor(mon.client, db, mon.sender, mon.cfg)
    mon2._auth_hour_ts.clear()
    stats = asyncio.run(mon2.poll_once(source="test"))
    assert stats["quota_deferred"] >= 1
    assert db.job_status(102) in (None, "new")
    db.close()


def test_automation_last_flag_derived_from_legacy_db():
    """БД без automation_last_flag (до ревизии памяти эскалации): время флага
    выводим из паузы (until - ступень), чтобы счётчик не сбрасывался зря."""
    tmpdir = tempfile.mkdtemp(prefix="hirify_test_")
    db = Database(Path(tmpdir) / "t.db")
    db.add_subscriber(1, "@tester")
    db.set_setting("automation_flags", "2")
    until = time.time() + 3 * 3600
    db.set_setting("automation_until", str(int(until)))   # без last_flag — legacy

    cfg = Config(bot_token="x")
    cfg.auth_views_hour_max = 10**9
    cfg.auth_views_day_max = 10**9
    mon = Monitor(FakeClient(FEED_ITEMS, VACANCIES), db, FakeSender(), cfg)
    # count=2 → ступень 240 мин → флаг «был» ~4 ч назад (не 48+)
    assert 3.5 * 3600 < until - mon._automation_last_flag <= 4.1 * 3600
    assert db.get_setting("automation_last_flag", "") != ""  # выведенное сохранено
    # и heal НЕ сбросит счётчик (флаг «недавний»)
    mon._automation_healed([{"chat_id": 1}])
    assert mon._automation_flag_count == 2
    db.close()


# ------------------------------------------------------------------ свежесть


def _iso(dt: datetime) -> str:
    """created_at как отдаёт сайт: 2026-11-11T00:44:12.000000Z."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def test_stale_vacancies_skipped_between_polls_boundary():
    """Граница свежести = min(окно; прошлый проход − люфт):
    древнее (дни) не присылаем, а опубликованное МЕЖДУ проходами — присылаем,
    даже если оно старше жёсткого окна (ритм опроса не режет своё)."""
    now = datetime.now(timezone.utc)
    items = [
        # опубликована 8 дней назад — ровно тот случай, что жаловался пользователь
        {"id": 301, "slug": "301-ancient-role", "source": "telegram",
         "created_at": _iso(now - timedelta(days=8))},
        # опубликована 25 минут назад при прош-проходе 40 минут назад: между
        # проходами → свежая, хоть и старше жёсткого окна в 20 минут
        {"id": 302, "slug": "302-between-polls", "source": "telegram",
         "created_at": _iso(now - timedelta(minutes=25))},
    ]
    api = {"302-between-polls": (vacancy_json("Between Polls Role", 302),
                                contacts_json("hr"))}
    mon, _, _, db = make_monitor()
    mon.cfg.fresh_window_min = 20              # жёсткое окно меньше ритма опроса
    mon._last_poll_ts = time.time() - 40 * 60  # прошлый проход 40 минут назад
    spy = FakeClient(items, api)
    mon.client = spy

    stats = asyncio.run(mon.poll_once(source="test"))
    assert spy.job_fetches == 1               # древнюю не открывали вовсе
    assert db.job_status(301) == "skipped_stale"
    assert db.job_status(302) == "sent"       # межпроходная — доставлена
    assert stats["stale"] == 1 and stats["sent"] == 1
    db.close()


def test_cold_start_first_poll_keeps_hard_window():
    """Холодный старт (прошлого прохода нет) — только жёсткое окно:
    первый запуск не вываливает всю первую страницу как «новую»."""
    now = datetime.now(timezone.utc)
    items = [
        {"id": 303, "slug": "303-hour-old", "source": "telegram",
         "created_at": _iso(now - timedelta(hours=2))},
        {"id": 304, "slug": "304-just-posted", "source": "telegram",
         "created_at": _iso(now - timedelta(minutes=5))},
    ]
    api = {"304-just-posted": (vacancy_json("Just Posted Role", 304),
                              contacts_json("hr"))}
    mon, _, _, db = make_monitor()
    mon.cfg.fresh_window_min = 30
    mon._last_poll_ts = 0.0                   # прошлого прохода нет
    spy = FakeClient(items, api)
    mon.client = spy

    stats = asyncio.run(mon.poll_once(source="test"))
    assert db.job_status(303) == "skipped_stale"
    assert db.job_status(304) == "sent"
    assert stats["stale"] == 1
    db.close()


def test_fresh_window_disabled_sends_old_too():
    """FRESH_WINDOW_MIN=0 — прежнее поведение: старьё тоже уходит."""
    now = datetime.now(timezone.utc)
    items = [{"id": 305, "slug": "305-week-old", "source": "telegram",
              "created_at": _iso(now - timedelta(days=7))}]
    api = {"305-week-old": (vacancy_json("Week Old Role", 305), contacts_json("hr"))}
    mon, _, _, db = make_monitor()
    mon.cfg.fresh_window_min = 0
    spy = FakeClient(items, api)
    mon.client = spy

    stats = asyncio.run(mon.poll_once(source="test"))
    assert db.job_status(305) == "sent"
    assert stats.get("stale", 0) == 0
    db.close()


def test_feed_item_without_created_at_still_processed():
    """Нет created_at в элементе фида — свежесть не судим (fail-open)."""
    items = [{"id": 306, "slug": "306-no-date", "source": "telegram"}]
    api = {"306-no-date": (vacancy_json("No Date Role", 306), contacts_json("hr"))}
    mon, _, _, db = make_monitor()
    mon.cfg.fresh_window_min = 30
    spy = FakeClient(items, api)
    mon.client = spy

    stats = asyncio.run(mon.poll_once(source="test"))
    assert db.job_status(306) == "sent" and stats["stale"] == 0
    db.close()


def test_last_poll_ts_persists_across_monitor_restarts():
    """Момент прошлого прохода живёт в БД: новый ран эстафеты продолжает
    отсчёт свежести с того же места, а не с холодного старта."""
    mon, _, _, db = make_monitor()
    asyncio.run(mon.poll_once(source="test"))
    ts = float(db.get_setting("last_poll_ts", "0"))
    assert ts > 0
    # «перезапуск»: новый Monitor на той же БД подхватывает метку
    mon2 = Monitor(mon.client, db, mon.sender, mon.cfg)
    assert mon2._last_poll_ts == ts
    db.close()


# ------------------------------------------------------------- ТГ-каналы-источники

class FakeChannelClient:
    """Вместо ChannelClient: посты-заготовки без сети."""

    def __init__(self, posts_by_channel: dict[str, list[dict]]):
        self.posts_by_channel = posts_by_channel
        self.fetches = 0

    async def fetch_posts(self, name: str) -> list[dict]:
        self.fetches += 1
        return self.posts_by_channel.get(name, [])

    async def close(self) -> None:
        pass


VACANCY_POST = (
    "Senior Media Buyer (iGaming)\n"
    "Ставка: 3-5k$, казино-траф, betting-офферы, Facebook Ads.\n"
    "Опыт от 2 лет, работа удалённая.\n"
    "Резюме: @hr_igaming"
)
NEWS_POST = "Новости рынка: ставки выросли, подробности на сайте."
SEEKER_POST = "Ищу работу медиабайером, резюме в личке, рассмотрю вакансии."


def _ch_item(mid: int, text: str, created_at: str) -> dict:
    return {"id": mid, "created_at": created_at, "text": text}


def _landing(title: str, description: str, company: str = "Acme") -> str:
    """HTML публичной страницы вакансии: видимая страница прячет контакты
    («Show contacts»), но SEO-JSON-LD несёт полное описание — как у реального
    hirify (проверено живой пробой probe.yml)."""
    import json as _json
    ld = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": description,
        "hiringOrganization": {"name": company},
        "datePosted": "2026-11-11",
    }
    return (
        '<html><head><script type="application/ld+json">'
        + _json.dumps(ld)
        + f'</script></head><body><h1>{title}</h1>'
          '<div>Show contacts</div></body></html>'
    )


def test_anon_first_delivers_without_auth_requests():
    """Анонимный путь: контакт виден в SEO-JSON-LD публичной страницы →
    доставка БЕЗ единого запроса от имени аккаунта (анти-флаг)."""
    mon, client, sender, db = make_monitor()
    mon.cfg.tg_channels = ""
    client.feed_items = FEED_ITEMS[:1]
    client.landing_html["101-senior-solidity-engineer"] = _landing(
        "Solidity Engineer",
        "Мы ищем инженера. Контакты: Telegram @acme_crypto_hr "
        "или https://t.me/acme_crypto_hr",
    )
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["sent"] == 1 and stats["anon"] == 1
    assert client.landing_fetches == 1
    assert client.job_fetches == 0            # API вакансий не открывался вовсе
    assert db.job_status(101) == "sent"
    assert sender.sent and sender.sent[0][1].startswith("Solidity Engineer")
    db.close()


def test_anon_fallback_to_auth_when_public_page_has_no_contact():
    """Контакта на публичной странице нет → прежний auth-путь (API + контакты)."""
    mon, client, sender, db = make_monitor()
    mon.cfg.tg_channels = ""
    client.feed_items = FEED_ITEMS[:1]   # landing_html пуст — контакта нет
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["sent"] == 1 and stats["anon"] == 0
    assert client.landing_fetches == 1
    assert client.job_fetches == 1            # вакансия открыта через API
    assert db.job_status(101) == "sent"
    db.close()


def test_anon_delivers_during_automation_pause():
    """Флаг аккаунта глушит только auth-путь: вакансия с публичным контактом
    доставляется и во время паузы; без публичного контакта — ждёт ретрая
    (статус 'new', свежесть для неё расширена)."""
    mon, client, sender, db = make_monitor()
    mon.cfg.tg_channels = ""
    client.feed_items = FEED_ITEMS            # 101 — публичный контакт, 102/103 — нет
    client.landing_html["101-senior-solidity-engineer"] = _landing(
        "Solidity Engineer",
        "Контакты: Telegram @acme_crypto_hr или https://t.me/acme_crypto_hr",
    )
    mon._automation_until = time.time() + 3600
    mon._automation_last_flag = time.time()
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["automation_pause"] is True
    assert stats["sent"] == 1 and stats["anon"] == 1
    assert client.job_fetches == 0            # auth не тронут вовсе
    assert db.job_status(101) == "sent"
    assert db.job_status(102) == "new"        # отложена — вернётся ретраем
    db.close()


def test_fresh_threshold_extends_over_recent_automation_pause():
    """Публикации, застрявшие на время паузы флага, не режутся свежестью:
    пока флаг горячий, граница не позже момента флага. Остывший флаг —
    расширения нет (иначе бэклог фида выплеснулся бы как «свежий»)."""
    from app.monitor import FEED_FRESH_SLACK_SEC
    mon, _, _, db = make_monitor()
    mon.cfg.fresh_window_min = 30.0
    mon._last_poll_ts = 0                     # холодный старт — только жёсткое окно
    base = mon._fresh_threshold_ts()

    mon._automation_last_flag = time.time() - 3600   # флаг час назад (старше окна) — горячий
    wider = mon._fresh_threshold_ts()
    assert wider <= mon._automation_last_flag + FEED_FRESH_SLACK_SEC
    assert wider < base - 600

    mon._automation_last_flag = time.time() - 48 * 3600   # остывший — не влияет
    assert abs(mon._fresh_threshold_ts() - base) < 1.0
    db.close()


def _make_channel_monitor(posts: dict[str, list[dict]]):
    mon, client, sender, db = make_monitor()
    mon.cfg.tg_channels = "cpa_people"
    client.feed_items = []      # изолируем: только канал, без вакансий hirify
    mon._channels = FakeChannelClient(posts)
    return mon, client, sender, db


def test_channel_post_detected_sent_and_deduped():
    now = datetime.now(timezone.utc)
    posts = {"cpa_people": [_ch_item(1234, VACANCY_POST, _iso(now - timedelta(minutes=5)))]}
    mon, _, sender, db = _make_channel_monitor(posts)

    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["total"] == 1 and stats["sent"] == 1
    assert sender.sent and sender.sent[0][1].startswith("Senior Media Buyer")
    # job_id поста — отрицательный (не пересекается с hirify)
    assert db.job_status(channel_job_id("cpa_people", 1234)) == "sent"
    # второй проход — дедуп, повторной отправки нет
    sender.sent.clear()
    stats2 = asyncio.run(mon.poll_once(source="test"))
    assert stats2["total"] == 0 and sender.sent == []
    db.close()


def test_channel_news_and_seekers_skipped():
    now = datetime.now(timezone.utc)
    posts = {"cpa_people": [
        _ch_item(1301, NEWS_POST, _iso(now - timedelta(minutes=4))),
        _ch_item(1302, SEEKER_POST, _iso(now - timedelta(minutes=3))),
    ]}
    mon, _, sender, db = _make_channel_monitor(posts)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["total"] == 2 and stats["sent"] == 0
    assert db.job_status(channel_job_id("cpa_people", 1301)) == "skipped_not_vacancy"
    assert db.job_status(channel_job_id("cpa_people", 1302)) == "skipped_not_vacancy"
    assert sender.sent == []
    db.close()


def test_channel_stale_post_not_sent():
    now = datetime.now(timezone.utc)
    posts = {"cpa_people": [_ch_item(1234, VACANCY_POST, _iso(now - timedelta(days=3)))]}
    mon, _, sender, db = _make_channel_monitor(posts)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats.get("stale", 0) == 1 and stats["sent"] == 0 and sender.sent == []
    assert db.job_status(channel_job_id("cpa_people", 1234)) == "skipped_stale"
    db.close()


def test_channels_work_during_hirify_automation_pause():
    """Флаг автоматизации касается только hirify: каналы продолжают доставку."""
    now = datetime.now(timezone.utc)
    posts = {"cpa_people": [_ch_item(1234, VACANCY_POST, _iso(now - timedelta(minutes=5)))]}
    mon, client, sender, db = _make_channel_monitor(posts)
    # вакансия hirify тоже «новая» — есть что отложить паузой (иначе флаг не виден)
    client.feed_items = FEED_ITEMS
    mon._automation_until = time.time() + 3600
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["automation_pause"] is True   # вакансии hirify отложены
    assert stats["sent"] == 1                  # а пост канала доставлен
    assert sender.sent and sender.sent[0][1].startswith("Senior Media Buyer")
    # вакансии hirify НЕ открыты вовсе (не потрачены ни просмотры, ни квоты)
    assert client.job_fetches == 0
    db.close()


def test_channel_industry_filter_applies():
    now = datetime.now(timezone.utc)
    text = (
        "Оператор теплицы (агрокомплекс)\n"
        "Зарплата 40 000 ₽/мес, опыт от 1 года.\n"
        "Резюме: @agro_hr, работа на месте."
    )
    posts = {"cpa_people": [_ch_item(1401, text, _iso(now - timedelta(minutes=5)))]}
    mon, _, sender, db = _make_channel_monitor(posts)
    mon.cfg.industry_strict_tg = True   # флаг каналов включён принудительно
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["filtered"] == 1 and stats["sent"] == 0
    assert db.job_status(channel_job_id("cpa_people", 1401)) == "skipped_off_industry"
    db.close()


def test_channel_industry_filter_off_by_default():
    """INDUSTRY_STRICT_TG выключен по умолчанию: канал уже вертикально
    курируемый, общий INDUSTRY_RE поверх его постов не работает.
    Пост мимо INDUSTRY_RE (пейменты/CPA без «гемблинг»-слов) доставляется."""
    now = datetime.now(timezone.utc)
    text = (
        "Оператор теплицы (агрокомплекс)\n"
        "Зарплата 40 000 ₽/мес, опыт от 1 года.\n"
        "Резюме: @agro_hr, работа на месте."
    )
    posts = {"cpa_people": [_ch_item(1402, text, _iso(now - timedelta(minutes=5)))]}
    mon, _, sender, db = _make_channel_monitor(posts)
    assert mon.cfg.industry_strict_tg is False   # дефолт
    assert mon.cfg.industry_strict is False      # в тестовой фикстуре выключен — флаги независимы
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["sent"] == 1 and stats["filtered"] == 0
    assert db.job_status(channel_job_id("cpa_people", 1402)) == "sent"
    db.close()


def test_channel_recipient_feed_selection():
    """Пост канала уходит только подписчикам, включившим этот канал в /settings."""
    now = datetime.now(timezone.utc)
    posts = {"cpa_people": [_ch_item(1234, VACANCY_POST, _iso(now - timedelta(minutes=5)))]}
    mon, _, sender, db = _make_channel_monitor(posts)
    # единственный подписчик выбрал только раздел hirify — канал выключен
    mon.db.update_subscriber_settings(1, feeds=["remote"], silent=False)
    stats = asyncio.run(mon.poll_once(source="test"))
    assert stats["no_audience"] == 1 and sender.sent == []
    assert db.job_status(channel_job_id("cpa_people", 1234)) == "sent"  # финализировано
    db.close()
