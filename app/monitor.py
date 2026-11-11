"""Ядро: цикл мониторинга фида hirify.me и рассылка новых вакансий с прямыми TG-контактами."""
from __future__ import annotations

import asyncio
import logging
import time

from .db import Database
from .hirify import (
    HirifyAutomationError, HirifyAuthError, HirifyClient, HirifyError,
    HirifyNotFound, HirifyRestrictedError,
)
from .parsing import vacancy_from_api
from .sender import VacancySender

log = logging.getLogger("hirify.monitor")

COOKIE_WARN_COOLDOWN_SEC = 6 * 3600  # не чаще раза в 6 часов
NUDGE_COOLDOWN_SEC = 60              # /start не должен дёргать фид чаще раза в минуту
MAX_FETCH_ATTEMPTS = 3               # столько раз пробуем загрузить страницу вакансии
MAX_SEND_ATTEMPTS = 10               # потолок ретраев доставки (send_failed → failed)
# Сайт помечает «автоматизацию» → контакты закрыты на ~24 ч. Долбить дальше
# бессмысленно и вредно: ждём час-полтора и пробуем снова (вакансии не сгорают).
AUTOMATION_COOLDOWN_SEC = 90 * 60


class Monitor:
    def __init__(self, client: HirifyClient, db: Database, sender: VacancySender, config):
        self.cfg = config
        self.client = client
        self.db = db
        self.sender = sender
        self._last_cookie_warn = 0.0
        self._last_poll_ts = 0.0
        self._automation_until = 0.0    # до этого момента сайт просит «не автоматизировать»
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
        while self._running:
            wait = self.cfg.poll_minutes * 60
            if not self.paused:
                try:
                    stats = await self.poll_once(source="auto")
                    wait = self.next_interval(stats)
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
        fetch_failed, send_failed, no_audience, cookie_warn}.
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

    async def _poll_impl(self, source: str, max_jobs: int) -> dict:
        stats = self._new_stats()

        # 1) фид каждого раздела — из JSON-API: только вакансии с прямыми
        #    TG-контактами, серверная фильтрация (HTML-лендинг такое не умеет)
        links: list[tuple[int, str, str, object]] = []  # (job_id, slug, url, feed)
        for feed in self.cfg.feeds:
            for page in range(1, max(1, self.cfg.feed_pages) + 1):
                try:
                    items = await self.client.fetch_feed_api(feed, page)
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
                    links.append((job_id, short_slug, f"{self.cfg.hirify_base}/jobs/{full_slug}", feed))
                if len(items) < 15:  # в странице фида 15 вакансий — дальше пусто
                    break

        # 2) оставляем только те, что ещё не видели (дедуп по всем разделам сразу;
        #    send_failed/fetch_failed/new known_job_ids не вернёт — будет ретрай
        #    'new' покрывает рестарт посреди прохода: незавершённые не теряются)
        seen_in_run: set[int] = set()
        unique: list[tuple[int, str, str, object]] = []
        for row in links:
            if row[0] in seen_in_run:
                continue
            seen_in_run.add(row[0])
            unique.append(row)
        known = self.db.known_job_ids([job_id for job_id, _, _, _ in unique])
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
                     (self._automation_until - time.time()) / 60)
            return stats

        # 3) открываем каждую страницу вакансии, ищем контакты
        fresh = fresh[:max_jobs]
        log.info("[%s] новых вакансий: %d (лимит %d, разделы: %s)", source, len(fresh), max_jobs,
                 ", ".join(f.key for f in self.cfg.feeds))

        for job_id, slug, url, feed in fresh:
            self.db.add_job(job_id, slug, url)
            stats["total"] += 1
            try:
                # данные и контакты — из JSON-API (страницы вакансий рендерит
                # клиентский JS, парсить их бессмысленно; API отдаёт всё разом).
                # Слаг в фиде хранится без числового префикса — API ждёт полный.
                full_slug = f"{job_id}-{slug}"
                data = await self.client.fetch_vacancy(full_slug)
                contacts_data = await self.client.fetch_contacts(full_slug)
                v = vacancy_from_api(data, contacts_data, feed.key, feed.label)
            except HirifyAutomationError as e:
                # сайт пометил аккаунт: контакты закрыты ~на 24 ч. Длинная
                # пауза — вакансии остаются fetch_failed и вернутся позже,
                # попытки НЕ сгорают (bump не делаем)
                log.warning("Сайт заподозрил автоматизацию — пауза %d мин: %s",
                            AUTOMATION_COOLDOWN_SEC // 60, e)
                self._automation_until = time.time() + AUTOMATION_COOLDOWN_SEC
                self.db.mark_job(job_id, "fetch_failed")
                stats["fetch_failed"] += 1
                break
            except HirifyRestrictedError as e:
                # премиум-вакансия или лимит бесплатных просмотров контактов —
                # ретраить бессмысленно, финализируем без рассылки
                log.info("job %d: контакты недоступны (403) — пропускаю: %s", job_id, e)
                self.db.mark_job(job_id, "skipped_restricted")
                stats["filtered"] += 1
                continue
            except HirifyNotFound as e:
                log.warning("Вакансия недоступна (удалена?): %s", e)
                self.db.mark_job(job_id, "failed")
                stats["failed"] += 1
                continue
            except HirifyAuthError as e:
                # сессия не работает: без неё контакты не получить — предупредим
                # админа и оставим вакансию на ретрай (когда cookie оживёт)
                log.error("Сессия hirify.me не принимается (%s) — обновите cookie", e)
                stats["cookie_warn"] = True
                self._warn_cookie([s["chat_id"] for s in subscribers[:1]])
                self.db.mark_job(job_id, "fetch_failed")
                stats["fetch_failed"] += 1
                continue
            except HirifyError as e:
                # сеть/таймаут/5xx — попробуем в следующем проходе (до MAX_FETCH_ATTEMPTS)
                attempts = self.db.bump_job_attempts(job_id)
                if attempts >= MAX_FETCH_ATTEMPTS:
                    self.db.mark_job(job_id, "failed")
                    stats["failed"] += 1
                else:
                    self.db.mark_job(job_id, "fetch_failed")
                    stats["fetch_failed"] += 1
                log.warning("Не удалось загрузить %s (попытка %d/%d): %s",
                            url, attempts, MAX_FETCH_ATTEMPTS, e)
                continue
            except Exception:  # noqa: BLE001 — неожиданный формат данных/сбой парсинга
                # без этого ретраи 'new'-статуса могли бы зациклиться на битой вакансии
                log.exception("job %d: непредвиденная ошибка обработки — на ретрай", job_id)
                attempts = self.db.bump_job_attempts(job_id)
                if attempts >= MAX_FETCH_ATTEMPTS:
                    self.db.mark_job(job_id, "failed")
                    stats["failed"] += 1
                else:
                    self.db.mark_job(job_id, "fetch_failed")
                    stats["fetch_failed"] += 1
                continue

            # помеченные сайтом как скам/подозрительные — не рассылаем
            if data.get("is_scam") or data.get("is_potential_scam"):
                self.db.mark_job(job_id, "skipped_scam")
                stats["filtered"] += 1
                log.info("job %d: помечен как скам — пропускаю", job_id)
                continue

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
            # дейтинг/high-risk/NDA-проекты/крипта и смежные). Вакансии «обычных»
            # компаний не рассылаются — финализируем без доставки
            if self.cfg.industry_strict and not v.is_target_industry:
                self.db.mark_job(job_id, "skipped_off_industry")
                stats["filtered"] += 1
                log.debug("job %d: целевых индустрий нет — пропускаю", job_id)
                continue

            # рассылка: только тем, кто выбрал этот раздел в /settings
            recipients = self._recipients_for(v, subscribers)
            if not recipients:
                # вакансия валидна, но ни одному подписчику не интересна —
                # финализируем (иначе гоняла бы ретраями до бесконечности)
                self.db.mark_job(job_id, "sent")
                stats["no_audience"] += 1
                log.debug("job %d: подписчиков раздела «%s» нет — обработана без доставки",
                          job_id, v.feed_key)
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

    def _warn_cookie(self, recipients: list[int]) -> None:
        """Одно напоминание в 6 часов, если контакты выглядят скрытыми."""
        now = time.time()
        if now - self._last_cookie_warn < COOKIE_WARN_COOLDOWN_SEC:
            return
        self._last_cookie_warn = now
        from . import texts

        async def _task() -> None:
            for chat_id in recipients[:1]:
                try:
                    await self.sender.send_text(chat_id, texts.COOKIE_HINT)
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
