"""Конфигурация бота. Все настройки берутся из .env / переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- фиды

@dataclass(frozen=True)
class Feed:
    """Один источник вакансий.

    source:
      "hirify" — раздел сайта hirify.me; api_params — параметры JSON-API
      (GET /api/vacancies?{api_params}&{feed_params}&page=N):
        remote  → work_format=remote
        global  → remote_type=global
        crypto  → search=crypto (+ клиентская проверка крипто-признаков)
      "tg"     — публичный Telegram-канал (читается анонимно через
      t.me/s/<channel>); channel — имя канала без @ и ссылок.
    """

    key: str          # короткое имя: remote / global / tg_cpa_people …
    label: str        # человекочитаемая метка для карточки и статуса
    base_url: str     # адрес лендинга (HTML-фолбэк и человеческие ссылки)
    api_params: str = ""  # фильтр раздела для JSON-API (hirify)
    source: str = "hirify"  # "hirify" | "tg"
    channel: str = ""       # имя ТГ-канала (для source="tg")


# Готовые «разделы» hirify.me. Лендинги — те же фид-страницы сайта с
# предустановленным серверным фильтром (проверено живыми запросами к API):
#   remote  → «Удалёнка» (полностью удалённые IT-вакансии)
#   global  → «Глобальная удалёнка» (work from anywhere)
#   crypto  → «Crypto & Web3» (крипто-индустрия, любой формат)
#   crypto_remote → пересечение: крипта + удалёнка
#   all     → общий фид без раздела (прежнее поведение)
FEED_PRESETS: dict[str, Feed] = {
    "remote": Feed("remote", "🏠 Удалёнка", "https://hirify.me/remote-it-jobs",
                   api_params="work_format=remote"),
    "global": Feed("global", "🌍 Глобальная удалёнка", "https://hirify.me/remote-jobs",
                   api_params="remote_type=global"),
    "crypto": Feed("crypto", "🪙 Crypto & Web3", "https://hirify.me/web3-crypto-jobs",
                   api_params="search=crypto"),
    "crypto_remote": Feed("crypto_remote", "🪙🌍 Crypto Remote", "https://hirify.me/web3-crypto-remote-jobs",
                           api_params="search=crypto&work_format=remote"),
    "all": Feed("all", "🔎 Все вакансии", "https://hirify.me/"),
}


def parse_feeds(raw: str) -> list[Feed]:
    """'remote,global,crypto' → [Feed, Feed, Feed].

    Элемент может быть ключом пресета (remote/global/crypto/crypto_remote/all),
    путём ('/some-landing-jobs') или полным URL. Дубликаты выкидываем.
    """
    feeds: list[Feed] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        token = part.strip().strip("/")
        if not token:
            continue
        if token in FEED_PRESETS:
            feed = FEED_PRESETS[token]
        elif token.startswith("http"):
            feed = Feed(key=token, label="🔎 " + token.split("//")[-1].split("?")[0], base_url=token)
        else:
            feed = Feed(key=token, label="🔎 " + token, base_url="https://hirify.me/" + token)
        if feed.key in seen:
            continue
        seen.add(feed.key)
        feeds.append(feed)
    return feeds or [FEED_PRESETS[k] for k in ("remote", "global", "crypto")]


def _pretty_channel_label(name: str) -> str:
    """cpa_people → «Cpa People», payments_lenkep → «Payments Lenkep»."""
    return " ".join(w for w in name.replace("-", "_").split("_") if w).title()


def parse_tg_channels(raw: str) -> list[Feed]:
    """'cpa_people,opento_igaming' → каналы-источники (source="tg").

    Элемент — имя канала (с/без @, с/без https://t.me/… или t.me/s/…).
    Ключ — tg_<имя в нижнем регистре> (стабилен при любом регистра имён),
    подпись — человекочитаемая «📡 Cpa People». Пусто → [].
    """
    feeds: list[Feed] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        if "t.me/" in token:  # https://t.me/s/name | https://t.me/name | t.me/name
            token = token.split("t.me/", 1)[1]
            if token.startswith("s/"):
                token = token[2:]
        token = token.strip().strip("@").strip("/")
        if not token or " " in token:
            continue
        key = f"tg_{token.lower()}"
        if key in seen:
            continue
        seen.add(key)
        feeds.append(Feed(
            key=key,
            label=f"📡 {_pretty_channel_label(token)}",
            base_url=f"https://t.me/{token}",
            source="tg",
            channel=token,
        ))
    return feeds


@dataclass
class Config:
    """Все настройки бота."""

    # --- Telegram ---
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))

    # --- Hirify ---
    # Cookie-строка из браузера (см. README). Пусто = парсим без авторизации.
    hirify_cookie: str = field(default_factory=lambda: os.getenv("HIRIFY_COOKIE", "").strip())
    # Какие разделы hirify.me мониторим (через запятую):
    #   remote, global, crypto, crypto_remote, all  — или свой лендинг '/…'
    hirify_feeds: str = field(default_factory=lambda: os.getenv("HIRIFY_FEEDS", "remote,global,crypto"))
    # Публичные Telegram-каналы-источники (через запятую: имена/ссылки):
    # читаются АНОНИМНО через веб-превью t.me/s/<имя> — без аккаунта и без
    # бота в канале. Посты проходят тем же конвейером: детектор вакансии →
    # карточки → рассылка. Пусто (по умолчанию) = каналов нет; боевой список
    # задаётся окружением (bot.yml на Actions: cpa_people, opento_igaming,
    # payments_lenkep, IT_iGaming).
    tg_channels: str = field(default_factory=lambda: os.getenv("TG_CHANNELS", "").strip())
    # Общие параметры фида (добавляются к каждому разделу).
    # Полный список: см. README (only_direct_contact, contact_types, countries,
    # salary_from, grade, sort_by, page и т.д.)
    feed_params: str = field(
        default_factory=lambda: os.getenv(
            "HIRIFY_FEED_PARAMS",
            "only_direct_contact=1&contact_types=telegram&sort_by=newest",
        ).lstrip("?")
    )
    hirify_base: str = field(default_factory=lambda: os.getenv("HIRIFY_BASE", "https://hirify.me").rstrip("/"))
    # JSON-API сайта (Laravel Sanctum, отдельный домен): вакансии и контакты
    hirify_api_base: str = field(default_factory=lambda: os.getenv("HIRIFY_API_BASE", "https://api.hirify.me").rstrip("/"))

    # Строгая клиентская проверка поверх серверных лендингов:
    # remote_strict  — выкидывать вакансию, если на её странице явно указан
    #                  офисный/гибридный формат (пропускаем без признаков —
    #                  лендинг уже отфильтровал);
    # crypto_strict  — в разделе crypto оставлять только вакансии с явными
    #                  крипто-признаками (заголовок/навыки/описание).
    remote_strict: bool = field(default_factory=lambda: _get_bool("REMOTE_STRICT", True))
    crypto_strict: bool = field(default_factory=lambda: _get_bool("CRYPTO_STRICT", True))
    # Строгий фильтр целевых индустрий: рассылать ТОЛЬКО вакансии из iGaming /
    # гемблинга / беттинга, дейтинга, high-risk- и NDA-проектов, крипты и смежных
    # (признаки — INDUSTRY_RE в parsing.py). Обычные компании отсеиваются.
    industry_strict: bool = field(default_factory=lambda: _get_bool("INDUSTRY_STRICT", True))
    # Тот же фильтр для ПОСТОВ ТГ-КАНАЛОВ — по умолчанию ВЫКЛЮЧЕН. Каналы уже
    # вертикально курируемые (cpa_people — CPA, betting_job — беттинг и т.п.),
    # а INDUSTRY_RE знает только гемблинг/дейтинг/крипту: поверх их контента
    # он выкидывал ровно то, ради чего каналы подключали — половину постов
    # cpa_people (медиабаеры, пейменты, CPA-менеджеры без «гемблинг»-слова).
    # 1 = вернуть общий фильтр и для каналов.
    industry_strict_tg: bool = field(default_factory=lambda: _get_bool("INDUSTRY_STRICT_TG", False))
    # Дешёвый префильтр по элементу фида (title+tldr+company+стек, БЕЗ
    # открытия вакансии): вакансии мимо целевых индустрий не открываем вовсе —
    # именно «просмотры» аккаунта сайт считает и за них флажит автоматизацию.
    # Элементы без title/tldr всё равно открываем (нечем судить).
    feed_prefilter: bool = field(default_factory=lambda: _get_bool("FEED_PREFILTER", True))
    # Квоты на запросы ОТ ИМЕНИ АККАУНТА (данные вакансии + контакты): лимит
    # в час и в сутки. Превышение — остальные вакансии откладываются до
    # следующего прохода (не теряются: фид анонимный, ретрай бесплатный).
    # Аккаунт, просматривающий десятки вакансий подряд из IP датацентра —
    # классический триггер automation_detected.
    auth_views_hour_max: int = field(default_factory=lambda: _get_int("AUTH_VIEWS_HOUR_MAX", 12))
    auth_views_day_max: int = field(default_factory=lambda: _get_int("AUTH_VIEWS_DAY_MAX", 80))

    # Пауза между загрузками страниц фида, сек (вежливость к сайту).
    # Паузы «человеческого» темпа для дорогих запросов (диапазоны «lo,hi»,
    # сек): вакансию человек читает десяток секунд, контакты открывает
    # не мгновенно. Ровные 1.5 с между открытиями вакансий — метроном,
    # каким джоб-борды люди не листают.
    fetch_delay_sec: float = field(default_factory=lambda: _get_float("FETCH_DELAY_SEC", 1.5))
    vacancy_gap_sec: str = field(default_factory=lambda: os.getenv("VACANCY_GAP_SEC", "6,14"))
    contacts_gap_sec: str = field(default_factory=lambda: os.getenv("CONTACTS_GAP_SEC", "4,9"))
    request_timeout_sec: float = field(default_factory=lambda: _get_float("REQUEST_TIMEOUT_SEC", 30.0))
    # Двигатель запросов: curl_cffi с имперсонацией Chrome — настоящий
    # TLS-отпечаток (JA3) и HTTP/2, как у браузера, вместо «питоновского»
    # TLS у httpx. Цель имперсонации: chrome (последняя известная библиотеке)
    # или конкретная — chrome142, safari17_0 и т.д.
    impersonate_target: str = field(default_factory=lambda: os.getenv("IMPERSONATE_TARGET", "chrome"))

    # --- Мониторинг ---
    # Базовый интервал проверки фида, минут. Держим небольшим: карточка должна
    # приходить почти сразу после появления вакансии. Реальный ритм слегка
    # плавает (джиттер в monitor.py: ×0.8–1.35 от базового) — «ровно каждые N
    # минут, круглые сутки» — подпись бота, на которой сайты ловят автоматизацию.
    poll_minutes: float = field(default_factory=lambda: _get_float("POLL_MINUTES", 5.0))
    # Быстрая повторная проверка после прохода, в котором было новое (сек):
    # так «пачка» вакансий уходит подряд, а не ждёт следующего цикла.
    fast_poll_sec: float = field(default_factory=lambda: _get_float("FAST_POLL_SEC", 120.0))
    max_per_poll: int = field(default_factory=lambda: _get_int("MAX_PER_POLL", 8))
    # Сколько страниц каждого фида просматривать за один цикл
    feed_pages: int = field(default_factory=lambda: _get_int("FEED_PAGES", 1))
    # Глубина «метлы» после паузы автоматизации: пока сайт держал флаг, в фиде
    # копятся вакансии, и хвост может уйти со страницы 1 навсегда — первый
    # проход после снятия ограничения проходит столько страниц каждого фида.
    catchup_feed_pages: int = field(default_factory=lambda: _get_int("CATCHUP_FEED_PAGES", 4))
    # Свежесть вакансий, мин: «новая для бота» ≠ «недавно опубликованная».
    # Фид с only_direct_contact обновляется редко — на странице 1 висят
    # вакансии возрастом дни; без окна бот присылает их как «новые». Всё,
    # что опубликовано старше окна (или раньше прошлого прохода фида минус
    # люфт — публикации МЕЖДУ проходами всегда доставляются), помечается
    # skipped_stale и не рассылается. 0 = выключить (присылать и старьё).
    fresh_window_min: float = field(default_factory=lambda: _get_float("FRESH_WINDOW_MIN", 30.0))

    # --- Отправка ---
    # 1 = пробовать Rich Messages (Bot API 10.1+, красиво), с автооткатом на обычный HTML
    use_rich: bool = field(default_factory=lambda: _get_bool("USE_RICH", True))
    # 1 = разрешить HTML-виджеты (Bot API 10.4+, Telegram 13.2+) для подписчиков,
    # выбравших «Пузырь» в /settings; при сбое — откат на rich/классику
    use_widget: bool = field(default_factory=lambda: _get_bool("USE_WIDGET", True))
    # Лимиты описаний карточек НЕ настраиваются — автобюджет под лимиты Telegram:
    # rich — до 32768 символов html (полное описание в <details>), классика —
    # 4096 видимых символов (<blockquote expandable>), виджет — 32 КБ html /
    # 4096 байт zlib-9. См. app/cards.py и app/bubble.py.
    # Пауза между отправками сообщений (flood-контроль), сек
    send_delay_sec: float = field(default_factory=lambda: _get_float("SEND_DELAY_SEC", 1.2))

    # --- Служебное ---
    # ID чата администратора (личный чат владельца бота): если задан, критичные
    # команды (/setcookie, /pause, /resume) принимаются только от него — без
    # этого любой пользователь, нашедший бота, сможет подменить cookie или
    # остановить мониторинг для всех. 0 = гарда нет (личный бот).
    admin_chat_id: int = field(default_factory=lambda: _get_int("ADMIN_CHAT_ID", 0))
    db_path: Path = field(default_factory=lambda: BASE_DIR / os.getenv("DB_PATH", "data/hirify_bot.db"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    debug_dump_dir: Path = field(default_factory=lambda: BASE_DIR / "debug")

    # --- Хостинг с автоперезапуском (GitHub Actions и т.п.) ---
    # Лимит времени работы, минут: после него бот ПЛАВНО остананавливается
    # (monitor.stop + отменяем поллинг). Нужен на GitHub Actions, где job живёт
    # максимум 6 часов: выходим заранее, воркфлоу успевает сохранить БД и
    # перезапустить сам себя. 0 = без лимита (обычный режим «жить вечно»).
    run_max_minutes: float = field(default_factory=lambda: _get_float("RUN_MAX_MINUTES", 0.0))
    # Сбрасывать ли накопившиеся апдейты Telegram при старте (после долгого
    # офлайна пачка старых команд только мешает). В режиме Actions выключаем:
    # команды, присланные в минутный зазор между ранами, не должны теряться.
    drop_pending_updates: bool = field(default_factory=lambda: _get_bool("DROP_PENDING_UPDATES", True))

    @property
    def feeds(self) -> list[Feed]:
        """Активные источники мониторинга: разделы hirify + ТГ-каналы."""
        return parse_feeds(self.hirify_feeds) + parse_tg_channels(self.tg_channels)

    def feed_url_for_page(self, feed: Feed, page: int) -> str:
        """URL лендинга раздела с общими параметрами и номером страницы."""
        params = "&".join(
            p for p in self.feed_params.split("&") if p and not p.startswith("page=")
        )
        sep = "&" if "?" in feed.base_url else "?"
        return f"{feed.base_url}{sep}{params}&page={page}"


def load_config() -> Config:
    """Читает .env рядом с проектом и возвращает конфиг."""
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    cfg = Config()
    if not cfg.bot_token:
        # Токен можно передать и просто переменной окружения
        cfg.bot_token = os.getenv("BOT_TOKEN", "")
    return cfg
