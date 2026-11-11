"""Ядро: цикл мониторинга фида hirify.me и рассылка новых вакансий с прямыми TG-контактами."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from .db import Database
from .hirify import (
    HirifyAutomationError, HirifyAuthError, HirifyClient, HirifyError,
    HirifyNotFound, HirifyRestrictedError,
)
from .parsing import CRYPTO_RE, INDUSTRY_RE, NON_REMOTE_RE, vacancy_from_api
from .sender import VacancySender

log = logging.getLogger("hirify.monitor")

COOKIE_WARN_COOLDOWN_SEC = 6 * 3600  # не чаще раза в 6 часов
AUTOMATION_WARN_COOLDOWN_SEC = 6 * 3600
NUDGE_COOLDOWN_SEC = 60              # /start не должен дёргать фид чаще раза в минуту
MAX_FETCH_ATTEMPTS = 3               # столько раз пробуем загрузить страницу вакансии
MAX_SEND_ATTEMPTS = 10               # потолок ретраев доставки (send_failed → failed)
# Сайт помечает «автоматизацию» → доступ к вакансиям закрыт на ~24 ч. Долбить
# дальше бессмысленно и, похоже, вредно (каждая попытка может продлевать
# ограничение), поэтому пауза эскалируется с каждым повторным срабатыванием.
# Живёт пауза в БД: раны замещают друг друга — в памяти она бы терялась, и
# каждый новый ран снова дёргал бы закрытый API через минуту после старта.
AUTOMATION_BACKOFF_MIN = (90, 240, 480, 960, 1440)   # мин, с эскалацией
# Память эскалации: повторный флаг в течение этого окна после прошлого —
# счётчик эскалации НЕ сбрасывается «излечением» (сайт снял флаг ≠ урок
# усвоен). Полный сброс — только если флагов не было 48 ч подряд.
AUTOMATION_FLAG_MEMORY_SEC = 48 * 3600
# Пока сайт держит флаг, фиды тоже не дёргаем каждые 5 мин: раз в 30 мин
# хватит (бэклог копится и так), зато ограниченный аккаунт не палится
# непрерывным поллингом — возможно, каждая попытка продлевает ограничение.
# К концу паузы просыпаемся точно, чтобы сразу добрать накопленное.
AUTOMATION_POLL_SEC = 30 * 60
# Джиттер интервала опроса: «ровно каждые N минут» — классическая подпись
# бота; человек ходит по сайту неровно. X0.8–1.35 от базового интервала.
POLL_JITTER = (0.8, 1.35)
# Суточный ритм человека: ночью (01:00–06:59 МСК) мы спим — «тихие» проходы
# удлиняются втрое; утром первый проход за день глубокий («метла», та же
# что после паузы автоматизации) — собирает ночной бэклог с запасом страниц.
# Это НЕ маскировка под человека, а честное расписание: меньше ночных
# запросов — меньше поводов для флага и меньше нагрузки на сайт.
NIGHT_HOURS = (1, 7)    # МСК: [1, 7) — «спим»
NIGHT_SLOWDOWN = 3.0


def _msk_hour(ts: float | None = None) -> int:
    """Час по МСК (UTC+3 без переходов на летнее время) — для суточного ритма."""
    return int((time.time() if ts is None else ts) // 3600 + 3) % 24


class Monitor:
    def __init__(self, client: HirifyClient, db: Database, sender: VacancySender, config):
        self.cfg = config
        self.client = client
        self.db = db
        self.sender = sender
        self._last_cookie_warn = 0.0
        self._last_automation_warn = 0.0
        self._last_poll_ts = 0.0
        # пауза после флага автоматизации — из БД (переживает замещение ранов)
        self._automation_until = self._load_ts("automation_until")
        self._automation_flag_count = int(self._load_ts("automation_flags"))
        self._automation_last_flag = self._load_ts("automation_last_flag")
        # квоты запросов от имени аккаунта: час — в памяти, сутки — в БД
        # (переживают замещение ранов, как и пауза автоматизации)
        self._auth_hour_ts: list[float] = []
        self._auth_day = int(self._load_ts("auth_views_count"))
        self._auth_day_date = (self.db.get_setting("auth_views_date") or "").strip()
        self._pause_just_expired = False
        self._running = False
        self._busy = False
        self._wake = asyncio.Event()
        self._bg_tasks: set[asyncio.Task] = set()  # сильные ссылки на fire-and-forget задачи

    @property
    def busy(self) -> bool:
        """Идёт ли прямо сейчас проход poll_once (чтобы не запускать параллельно)."""
        return self._busy

    @property
    def last_poll_ts(self) -> float:
        """Когда закончился последний проход (для кулдауна /fresh)."""
        return self._last_poll_ts

    @property
    def automation_left_sec(self) -> float:
        """Сколько ещё держится пауза после флага автоматизации (для /status и /fresh)."""
        return max(0.0, self._automation_until - time.time())

    # ---------------------------------------------------------------- API

    @property
    def paused(self) -> bool:
        return self.db.get_setting("paused", "0") == "1"

    def pause(self) -> None:
        self.db.set_setting("paused", "1")

    def resume(self) -> None:
        self.db.set_setting("paused", "0")
        self.nudge()  # проснулись — проверяем фид сразу, без ожидания интервала

    def nudge(self) -> None:
        """Попросить цикл проверить фид при первой возможности (не чаще раза в минуту)."""
        if self._busy:
            return  # проход и так идёт — всё найдёт
        if time.time() - self._last_poll_ts < NUDGE_COOLDOWN_SEC:
            return
        self._wake.set()

    def next_interval(self, stats: dict) -> float:
        """Сколько ждать до следующего прохода.

        Ключ к «карточка приходит сразу, как только найдена": если в прошлом
        проходе было новое (отправили или отфильтровали) — пересматриваем фид
        уже через FAST_POLL_SEC секунд, а не через полный интервал.
        """
        if stats.get("total", 0) >= self.cfg.max_per_poll:
            return self.cfg.fast_poll_sec  # пачка не влезла в лимит — догоняем быстро
        if stats.get("sent", 0) > 0 or stats.get("total", 0) > 0:
            return self.cfg.fast_poll_sec  # было движение — вдруг появилось ещё
        return self.cfg.poll_minutes * 60

    def _post_poll_wait(self, stats: dict) -> float:
        """Интервал сна после прохода — с «человеческим» джиттером и суточным ритмом.

        Ровный метроном «раз в N минут, круглые сутки» — верный способ
        получить флаг automation_detected: так не browse'ит ни один человек.
        Поэтому базовый интервал гуляет (×0.8–1.35), а ночью (01:00–07:00 МСК)
        «тихие» проходы удлиняются втрое — как спящий человек. Быстрые
        повторы после находок не замедляются: ночной «хвост пачки» тоже
        надо добрать. Пока сайт держит флаг — фиды дёргаем редко и
        просыпаемся точно к концу паузы.
        """
        if stats.get("automation_pause"):
            left = self.automation_left_sec
            if left > 0:
                return min(AUTOMATION_POLL_SEC, left + 30.0)
        base = self.next_interval(stats)
        if base >= self.cfg.poll_minutes * 60:
            base *= self._night_slowdown()  # ночью «спим» — интервал ×3
        return max(60.0, base * random.uniform(*POLL_JITTER))

    def _night_slowdown(self) -> float:
        """Во сколько раз удлинять «тихие» ночные проходы (днём — 1.0)."""
        h = _msk_hour()
        return NIGHT_SLOWDOWN if NIGHT_HOURS[0] <= h < NIGHT_HOURS[1] else 1.0

    @staticmethod
    def _today_msk() -> str:
        """Дата (YYYY-MM-DD) по МСК — метка «сегодня уже мели»."""
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3 * 3600))

    def _morning_sweep_due(self) -> bool:
        """Первый за день проход ПОСЛЕ ночи (час ≥ 7 МСК) и глубокая проверка
        сегодня ещё не была. Ночью и до неё — False: разборка ночного бэклога
        на утреннем проходе, а не среди ночи.
        """
        if _msk_hour() < NIGHT_HOURS[1]:
            return False
        return self.db.get_setting("deep_sweep_date", "") != self._today_msk()

    async def _sleep(self, seconds: float) -> None:
        """Сон, который можно прервать: /resume, /start (nudge) или stop()."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(1.0, seconds))
        except asyncio.TimeoutError:
            pass
        self._wake.clear()  # гасим событие после ожидания, чтобы не потерять nudge

    async def run_forever(self) -> None:
        """Фоновая задача: адаптивный цикл. Запускается из main().

        Обычный ритм — раз в POLL_MINUTES минут; сразу после находки —
        повторная проверка через FAST_POLL_SEC секунд, чтобы новые карточки
        уходили практически в момент появления на сайте.
        """
        self._running = True
        log.info(
            "Мониторинг запущен: базовый интервал %.0f мин., повтор после находки — через %.0f c",
            self.cfg.poll_minutes, self.cfg.fast_poll_sec,
        )
        if self._automation_until > time.time():
            log.warning(
                "Подхватил паузу после флага автоматизации из БД (ещё %.0f мин) — вакансии подождут",
                self.automation_left_sec / 60,
            )
        while self._running:
            wait = self.cfg.poll_minutes * 60
            if not self.paused:
                try:
                    stats = await self.poll_once(source="auto")
                    wait = self._post_poll_wait(stats)
                except Exception:  # noqa: BLE001
                    log.exception("Цикл мониторинга упал с ошибкой")
            await self._sleep(wait)
        log.info("Мониторинг остановлен")

    def stop(self) -> None:
        self._running = False
        self._wake.set()  # разбудить сон, чтобы цикл завершился сразу

    @staticmethod
    def _new_stats() -> dict:
        return {
            "total": 0, "sent": 0, "skipped": 0, "filtered": 0, "failed": 0,
            "fetch_failed": 0, "send_failed": 0, "no_audience": 0, "cookie_warn": False,
            "automation_pause": False, "quota_deferred": 0,
        }

    @staticmethod
    def _recipients_for(v, subscribers: list[dict]) -> list[dict]:
        """Подписчики, которым интересен раздел этой вакансии.

        feeds=None — «по умолчанию» (все разделы бота); пустое множество —
        человек выключил всё; иначе — выбранные в /settings разделы.
        """
        out: list[dict] = []
        for s in subscribers:
            feeds = s.get("feeds")
            if feeds is not None and (v.feed_key or "") not in feeds:
                continue
            out.append(s)
        return out

    async def poll_once(self, source: str = "manual", limit: int | None = None) -> dict:
        """Один проход: фид → новые → парсинг → отправка подписчикам.

        Возвращает счётчики {total, sent, skipped, filtered, failed,
        fetch_failed, send_failed, no_audience, cookie_warn, automation_pause,
        quota_deferred}.
        """
        max_jobs = limit if limit is not None else self.cfg.max_per_poll
        if self._busy:
            log.warning("poll_once уже выполняется — пропускаю повторный запуск")
            return self._new_stats()
        self._busy = True
        try:
            return await self._poll_impl(source, max_jobs)
        finally:
            self._busy = False
            self._last_poll_ts = time.time()
            # «сняли флаг»-сообщение — только про СВЕЖЕИСТЕКШУЮ паузу, один раз
            self._pause_just_expired = False

    async def _poll_impl(self, source: str, max_jobs: int) -> dict:
        stats = self._new_stats()

        # 0) «метла» — глубокий первый проход (catchup_feed_pages), чтобы хвост
        # бэклога не ушёл со страницы 1 навсегда. Два повода, один раз в сутки:
        # (а) пауза автоматизации только что истекла — вакансии копились, пока
        # сайт держал флаг; (б) утро после ночного затишья — «человек проснулся
        # и пролистал ленту с запасом». Метка даты в БД — «метла» не
        # повторяется каждый проход и переживает замещение ранов.
        pages = self.cfg.feed_pages
        if 0 < self._automation_until <= time.time():
            pages = max(pages, self.cfg.catchup_feed_pages)
            self._automation_until = 0.0
            self.db.set_setting("automation_until", "0")
            self.db.set_setting("deep_sweep_date", self._today_msk())
            self._pause_just_expired = True  # для единственного «сняли флаг»-сообщения
            log.info("Пауза автоматизации истекла — прохожу фид «метлой»: %d стр. на раздел", pages)
        elif self._morning_sweep_due():
            pages = max(pages, self.cfg.catchup_feed_pages)
            self.db.set_setting("deep_sweep_date", self._today_msk())
            log.info("Утренний проход после ночи — фид «метлой»: %d стр. на раздел", pages)

        # 1) фид каждого раздела — из JSON-API АНОНИМНО (cookie не тратим:
        #    круглосуточный поллинг не должен накрывать аккаунт): только
        #    вакансии с прямыми TG-контактами, серверная фильтрация
        links: list[tuple[int, str, str, object, dict]] = []  # (job_id, slug, url, feed, item)
        for feed in self.cfg.feeds:
            for page in range(1, max(1, pages) + 1):
                try:
                    items = await self.client.fetch_feed_api(feed, page)
                except HirifyAutomationError as e:
                    # флаг прилетел уже на запросе ФИДА: без паузы монитор
                    # продолжал бы дёргать закрытый API каждые 5 минут.
                    # Регистрируем так же, как для вакансий: пауза с эскалацией
                    # + предупреждение админу, проход останавливаем.
                    subscribers = self.db.subscriber_settings()
                    self._register_automation(e, subscribers)
                    stats["automation_pause"] = True
                    return stats
                except HirifyError as e:
                    log.error("Не удалось загрузить фид «%s» (страница %d): %s", feed.key, page, e)
                    break
                for it in items:
                    try:
                        job_id = int(it.get("id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if not job_id:
                        continue
                    full_slug = str(it.get("slug") or "")
                    short_slug = full_slug.split("-", 1)[1] if "-" in full_slug else full_slug
                    links.append((job_id, short_slug, f"{self.cfg.hirify_base}/jobs/{full_slug}", feed, it))
                if len(items) < 15:  # в странице фида 15 вакансий — дальше пусто
                    break

        # 2) оставляем только те, что ещё не видели (дедуп по всем разделам сразу;
        #    send_failed/fetch_failed/new known_job_ids не вернёт — будет ретрай
        #    'new' покрывает рестарт посреди прохода: незавершённые не теряются)
        seen_in_run: set[int] = set()
        unique: list[tuple[int, str, str, object, dict]] = []
        for row in links:
            if row[0] in seen_in_run:
                continue
            seen_in_run.add(row[0])
            unique.append(row)
        known = self.db.known_job_ids([job_id for job_id, _, _, _, _ in unique])
        fresh = [row for row in unique if row[0] not in known]
        if not fresh:
            log.debug("Новых вакансий нет")
            return stats

        # без подписчиков открывать вакансии смысла нет — экономим запросы
        subscribers = self.db.subscriber_settings()
        if not subscribers:
            log.debug("Подписчиков нет — новые вакансии пропускаю (вернутся как ретрай)")
            return stats

        # после флага «автоматизация» даём сайту остыть: фиды смотрим (это
        # дёшево и не карается), но вакансии не трогаем до конца паузы
        if time.time() < self._automation_until:
            log.info("Пауза после флага автоматизации (ещё %.0f мин) — вакансии подождут",
                     self.automation_left_sec / 60)
            stats["automation_pause"] = True
            return stats

        # 3) открываем каждую страницу вакансии, ищем контакты
        fresh = fresh[:max_jobs]
        log.info("[%s] новых вакансий: %d (лимит %d, разделы: %s)", source, len(fresh), max_jobs,
                 ", ".join(f.key for f in self.cfg.feeds))

        for job_id, slug, url, feed, item in fresh:
            # квота запросов от имени аккаунта: сайт считает просмотры,
            # и именно пачки подряд — «автоматизация». Остальные вакансии
            # НЕ теряются: остаются невидимыми → следующий проход доберёт
            block = self._auth_quota_block()
            if block:
                stats["quota_deferred"] += 1
                log.info("Квота аккаунта (%s) — остальные %d вакансий подождут следующего прохода",
                         block, len(fresh) - stats["total"])
                break

            self.db.add_job(job_id, slug, url)
            stats["total"] += 1
            full_slug = f"{job_id}-{slug}"

            # ДЕШЁВЫЙ ПРЕФИЛЬТР: элемент фида уже содержит title/tldr/
            # стек/флаги скама — «мимо индустрии» видно без открытия
            # вакансии. Каждый пропущенный GET — минус один просмотр
            # аккаунта (и минус один повод для флага автоматизации).
            reason = self._prefilter_reason(item, feed)
            if reason:
                self.db.mark_job(job_id, reason)
                stats["filtered"] += 1
                log.debug("job %d: префильтр фида → %s (без открытия вакансии)", job_id, reason)
                continue

            # --- этап 1: данные вакансии (GET, от имени аккаунта). Фильтры
            # ниже решаемы уже по ним — контакты запрашиваем только для
            # прошедших отбор
            try:
                data = await self.client.fetch_vacancy(full_slug)
            except Exception as e:  # noqa: BLE001
                if self._handle_fetch_error(e, job_id, url, stats, subscribers) == "break":
                    break
                continue
            self._auth_view_spent()
            v = vacancy_from_api(data, None, feed.key, feed.label)

            # помеченные сайтом как скам/подозрительные — не рассылаем
            if data.get("is_scam") or data.get("is_potential_scam"):
                self.db.mark_job(job_id, "skipped_scam")
                stats["filtered"] += 1
                log.info("job %d: помечен как скам — пропускаю", job_id)
                continue

            self.db.update_job_parsed(job_id, title=v.title, company=v.company, salary=v.salary)

            # ФИЛЬТР 2: строгая проверка раздела (страховка поверх серверных лендингов)
            if feed.key in ("remote", "global", "crypto_remote") and self.cfg.remote_strict and v.is_non_remote:
                self.db.mark_job(job_id, "skipped_not_remote")
                stats["filtered"] += 1
                log.debug("job %d: формат «%s» — не удалёнка, пропускаю", job_id, v.work_format)
                continue
            if feed.key == "crypto" and self.cfg.crypto_strict and not v.is_crypto:
                self.db.mark_job(job_id, "skipped_not_crypto")
                stats["filtered"] += 1
                log.debug("job %d: крипто-признаков нет — пропускаю", job_id)
                continue

            # ФИЛЬТР 3: только целевые индустрии (iGaming/гемблинг/беттинг/
            # дейтинг/high-risk/NDA-проекты/крипта и смежные). Решается по
            # данным вакансии — БЕЗ запроса контактов: просмотры контактов
            # сайт считает, и именно они чаще всего и вызывают флаг «автоматизации»
            if self.cfg.industry_strict and not v.is_target_industry:
                self.db.mark_job(job_id, "skipped_off_industry")
                stats["filtered"] += 1
                log.debug("job %d: целевых индустрий нет — пропускаю", job_id)
                continue

            # рассылка: только тем, кто выбрал этот раздел в /settings. Если
            # адресатов нет — контакты не запрашиваем вовсе (просмотры не бесплатны)
            recipients = self._recipients_for(v, subscribers)
            if not recipients:
                # вакансия валидна, но ни одному подписчику не интересна —
                # финализируем (иначе гоняла бы ретраями до бесконечности)
                self.db.mark_job(job_id, "sent")
                stats["no_audience"] += 1
                log.debug("job %d: подписчиков раздела «%s» нет — обработана без доставки",
                          job_id, v.feed_key)
                continue

            # --- этап 2: контакты (POST, от имени аккаунта) — «дорогой» запрос,
            # делаем его только для целевой вакансии с адресатами
            try:
                contacts_data = await self.client.fetch_contacts(full_slug)
            except Exception as e:  # noqa: BLE001
                if self._handle_fetch_error(e, job_id, url, stats, subscribers) == "break":
                    break
                continue
            self._auth_view_spent()

            v = vacancy_from_api(data, contacts_data, feed.key, feed.label)
            # полный просмотр прошёл — флага больше нет; «сняли флаг»-
            # сообщение шлём только если пауза только что истекла (не каждый чих)
            self._automation_healed(subscribers, announce=self._pause_just_expired)
            self.db.update_job_parsed(
                job_id, title=v.title, company=v.company, salary=v.salary,
                tg_contacts=[{"kind": c.kind, "value": c.value, "display": c.display} for c in v.contacts],
            )

            # ФИЛЬТР 1: только вакансии с прямым Telegram-контактом
            # (фид уже фильтрует only_direct_contact — это страховка; проблемы
            # с самой сессией ловятся выше как HirifyAuthError)
            if not v.has_direct_contact:
                self.db.mark_job(job_id, "skipped_no_contact")
                stats["skipped"] += 1
                log.debug("job %d: TG-контакта нет — пропускаю", job_id)
                continue

            # считаем доставки — если НЕ УДАЛОСЬ НИ ОДНОЙ, вакансия
            # не финализируется (send_failed) и уйдёт ретраем в следующем проходе
            delivered = 0
            for s in recipients:
                try:
                    await self.sender.send_vacancy(
                        s["chat_id"], v,
                        silent=s["silent"],
                        display=s.get("display", "auto"),
                    )
                    delivered += 1
                except Exception as e:  # noqa: BLE001
                    log.error("Не отправилось сообщение в чат %s: %s", s["chat_id"], e)
            if delivered:
                self.db.mark_job(job_id, "sent")
                stats["sent"] += 1
            else:
                attempts = self.db.bump_job_attempts(job_id)
                if attempts >= MAX_SEND_ATTEMPTS:
                    self.db.mark_job(job_id, "failed")
                    stats["failed"] += 1
                    log.warning(
                        "job %d: доставка не удалась %d раз — снимаю с ретрая (failed)",
                        job_id, attempts,
                    )
                else:
                    self.db.mark_job(job_id, "send_failed")
                    stats["send_failed"] += 1
                    log.warning("job %d: ни одна отправка не прошла — повторю в следующем проходе", job_id)

        return stats

    # ---------------------------------------------------------------- helpers

    def _prefilter_reason(self, item: dict, feed) -> Optional[str]:
        """Дешёвый отбор ПО ЭЛЕМЕНТУ ФИДА, без открытия вакансии.

        Элемент фида уже содержит title/tldr/company_title/main_stack/tags/
        work_format и флаги скама — те же регекспы, что работают по полным
        данным (INDUSTRY_RE/CRYPTO_RE/NON_REMOTE_RE), можно применить заранее.
        Возврат — статус-причина (mark_job) или None («открываем как раньше»).

        Осторожность: элемент без title и tldr не судим (откроем — вдруг
        индустрия видна только в полном описании), и префильтр можно
        выключить env-ом FEED_PREFILTER=0.
        """
        if not self.cfg.feed_prefilter:
            return None
        if item.get("is_scam") or item.get("is_potential_scam"):
            return "skipped_scam"

        title = str(item.get("title") or item.get("original_title") or "")
        tldr = str(item.get("tldr") or "")
        if not (title.strip() or tldr.strip()):
            return None  # нечем судить — открываем, как раньше

        company = str(item.get("company_title") or "")
        wf = item.get("work_format")
        wf_s = ", ".join(x for x in wf if x) if isinstance(wf, list) else str(wf or "")

        # стек и теги — те же слова, что смотрит полный фильтр (skills)
        skills: list[str] = []
        stack = item.get("main_stack")
        if isinstance(stack, str):
            skills.append(stack)
        elif isinstance(stack, list):
            skills.extend(str(x) for x in stack if x)
        for t in item.get("tags") or []:
            if isinstance(t, dict):
                name = t.get("name")
                if name:
                    skills.append(str(name))
            elif t:
                skills.append(str(t))

        blob = " ".join(x for x in [title, company, " ".join(skills), tldr] if x)

        # порядок — как у полных фильтров ниже: не-удалёнка → не-крипта → индустрия
        if feed.key in ("remote", "global", "crypto_remote") and self.cfg.remote_strict \
                and NON_REMOTE_RE.search(wf_s):
            return "skipped_not_remote"
        if feed.key == "crypto" and self.cfg.crypto_strict and not CRYPTO_RE.search(blob):
            return "skipped_not_crypto"
        if self.cfg.industry_strict and not (INDUSTRY_RE.search(blob) or CRYPTO_RE.search(blob)):
            return "skipped_off_industry"
        return None

    def _auth_quota_block(self) -> Optional[str]:
        """Лимит запросов от имени аккаунта исчерпан? Причина или None.

        Часовое окно — в памяти (скользящее), суточное — в БД с датой по МСК:
        раны замещают друг друга каждые ~5.5 ч, счётчик должен переживать.
        """
        now = time.time()
        self._auth_hour_ts = [t for t in self._auth_hour_ts if now - t < 3600.0]
        hour_max = max(1, self.cfg.auth_views_hour_max)
        if len(self._auth_hour_ts) >= hour_max:
            return f"часовой лимит просмотров {hour_max} исчерпан"
        today = self._today_msk()
        if today != self._auth_day_date:  # новые сутки — новый счётчик
            self._auth_day_date = today
            self._auth_day = 0
            self.db.set_setting("auth_views_date", today)
            self.db.set_setting("auth_views_count", "0")
        day_max = max(1, self.cfg.auth_views_day_max)
        if self._auth_day >= day_max:
            return f"суточный лимит просмотров {day_max} исчерпан"
        return None

    def _auth_view_spent(self) -> None:
        """Учесть один «дорогой» запрос от имени аккаунта (вакансия/контакты)."""
        self._auth_hour_ts.append(time.time())
        self._auth_day += 1
        self.db.set_setting("auth_views_count", str(self._auth_day))

    def _load_ts(self, key: str) -> float:
        """Число из settings (пусто/мусор → 0): пауза автоматизации живёт в БД."""
        raw = (self.db.get_setting(key) or "0").strip() or "0"
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _handle_fetch_error(
        self, e: Exception, job_id: int, url: str, stats: dict, subscribers: list[dict],
    ) -> str:
        """Единая обработка ошибок загрузки данных/контактов вакансии.

        Возвращает действие для цикла: 'break' — сайт пометил автоматизацию,
        проход останавливаем до конца паузы; 'continue' — вакансия учтена,
        идём дальше.
        """
        if isinstance(e, HirifyAutomationError):
            # сайт пометил аккаунт: вакансии закрыты ~на 24 ч. Пауза с
            # эскалацией и в БД — попытки НЕ сгорают (bump не делаем)
            self._register_automation(e, subscribers)
            self.db.mark_job(job_id, "fetch_failed")
            stats["fetch_failed"] += 1
            return "break"
        if isinstance(e, HirifyRestrictedError):
            # премиум-вакансия или лимит бесплатных просмотров контактов —
            # ретраить бессмысленно, финализируем без рассылки
            log.info("job %d: контакты недоступны (403) — пропускаю: %s", job_id, e)
            self.db.mark_job(job_id, "skipped_restricted")
            stats["filtered"] += 1
            return "continue"
        if isinstance(e, HirifyNotFound):
            log.warning("Вакансия недоступна (удалена?): %s", e)
            self.db.mark_job(job_id, "failed")
            stats["failed"] += 1
            return "continue"
        if isinstance(e, HirifyAuthError):
            # сессия не работает: без неё контакты не получить — предупредим
            # админа и оставим вакансию на ретрай (когда cookie оживёт)
            log.error("Сессия hirify.me не принимается (%s) — обновите cookie", e)
            stats["cookie_warn"] = True
            self._warn_cookie([s["chat_id"] for s in subscribers[:1]])
            self.db.mark_job(job_id, "fetch_failed")
            stats["fetch_failed"] += 1
            return "continue"
        # сеть/таймаут/5xx/битый формат данных — ретрай в следующем проходе
        attempts = self.db.bump_job_attempts(job_id)
        if attempts >= MAX_FETCH_ATTEMPTS:
            self.db.mark_job(job_id, "failed")
            stats["failed"] += 1
        else:
            self.db.mark_job(job_id, "fetch_failed")
            stats["fetch_failed"] += 1
        if isinstance(e, HirifyError):
            log.warning("Не удалось загрузить %s (попытка %d/%d): %s",
                        url, attempts, MAX_FETCH_ATTEMPTS, e)
        else:
            # без этого ретраи 'new'-статуса могли бы зациклиться на битой вакансии
            log.exception("job %d: непредвиденная ошибка обработки — на ретрай", job_id)
        return "continue"

    def _register_automation(self, e: Exception, subscribers: list[dict]) -> int:
        """Флаг automation_detected: эскалирующая пауза (живёт в БД, переживает
        замещение ранов) + предупреждение админу. Возвращает длительность, мин.

        Вызывается и для вакансий (_handle_fetch_error), и для фидов (_poll_impl):
        без этого пауза не ставилась бы, если сайт закрыл доступ уже на запросе фида.
        """
        count = self._automation_flag_count + 1
        minutes = AUTOMATION_BACKOFF_MIN[min(count - 1, len(AUTOMATION_BACKOFF_MIN) - 1)]
        self._automation_flag_count = count
        self._automation_until = time.time() + minutes * 60
        self._automation_last_flag = time.time()
        self.db.set_setting("automation_until", str(int(self._automation_until)))
        self.db.set_setting("automation_flags", str(count))
        self.db.set_setting("automation_last_flag", str(int(self._automation_last_flag)))
        log.warning("Сайт заподозрил автоматизацию (срабатывание №%d) — пауза %d мин: %s",
                    count, minutes, e)
        self._warn_automation([s["chat_id"] for s in subscribers[:1]], minutes)
        return minutes

    def _automation_healed(self, subscribers: list[dict], announce: bool = False) -> None:
        """Успешный полный просмотр после флага — сайт снял ограничение.

        Счётчик эскалации сбрасываем ТОЛЬКО если с последнего флага прошло
        больше AUTOMATION_FLAG_MEMORY_SEC (48 ч): иначе сайт, снимающий флаг
        на пару часов и флажащий снова, каждый раз получал бы минимальную
        паузу 90 мин — лестница эскалации не работала бы вовсе. Пока счётчик
        держится, каждый следующий флаг сразу бьёт ступенью выше.
        Сообщение «сняли флаг» — только при announce (свежеистёкшая пауза),
        чтобы не спамить при каждом успешном просмотре.
        """
        if not self._automation_flag_count:
            return
        elapsed = time.time() - self._automation_last_flag
        self._automation_until = 0.0
        self.db.set_setting("automation_until", "0")
        if elapsed >= AUTOMATION_FLAG_MEMORY_SEC:
            self._automation_flag_count = 0
            self.db.set_setting("automation_flags", "0")
            log.info("Флаг автоматизации снят сайтом (тишина %.1f ч) — сброс счётчика эскалации",
                     elapsed / 3600)
        else:
            log.info("Флаг автоматизации снят сайтом, но сработал %.1f ч назад — счётчик "
                     "эскалации (%d) держу до 48 ч тишины", elapsed / 3600,
                     self._automation_flag_count)
        if announce:
            from . import texts

            self._fire_and_forget_text(
                [s["chat_id"] for s in subscribers[:1]], texts.AUTOMATION_LIFTED,
            )

    def _warn_cookie(self, recipients: list[int]) -> None:
        """Одно напоминание в 6 часов, если контакты выглядят скрытыми."""
        now = time.time()
        if now - self._last_cookie_warn < COOKIE_WARN_COOLDOWN_SEC:
            return
        self._last_cookie_warn = now
        from . import texts

        self._fire_and_forget_text(recipients, texts.COOKIE_HINT)

    def _warn_automation(self, recipients: list[int], minutes: int) -> None:
        """Одно сообщение в 6 часов, если сайт держит флаг автоматизации."""
        now = time.time()
        if now - self._last_automation_warn < AUTOMATION_WARN_COOLDOWN_SEC:
            return
        self._last_automation_warn = now
        from . import texts

        self._fire_and_forget_text(recipients, texts.AUTOMATION_HINT.format(minutes=minutes))

    def _fire_and_forget_text(self, recipients: list[int], html: str) -> None:
        """Отправить текстовое сообщение фоново (не роняя проход мониторинга)."""
        async def _task() -> None:
            for chat_id in recipients[:1]:
                try:
                    await self.sender.send_text(chat_id, html)
                except Exception:  # noqa: BLE001
                    pass

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_task())
            # держим сильную ссылку: fire-and-forget задачи может собрать GC
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            pass
