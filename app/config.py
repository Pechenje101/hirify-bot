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
    """Один источник вакансий (раздел hirify.me).

    api_params — дополнительные параметры API-фида раздела
    (GET /api/vacancies?{api_params}&{feed_params}&page=N):
      remote  → work_format=remote
      global  → remote_type=global
      crypto  → search=crypto (+ клиентская проверка крипто-признаков)
    """

    key: str          # короткое имя: remote / global / crypto …
    label: str        # человекочитаемая метка для карточки и статуса
    base_url: str     # адрес лендинга (HTML-фолбэк и человеческие ссылки)
    api_params: str = ""  # фильтр раздела для JSON-API


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

    # Пауза между загрузками страниц вакансий, сек (вежливость к сайту)
    fetch_delay_sec: float = field(default_factory=lambda: _get_float("FETCH_DELAY_SEC", 1.5))
    request_timeout_sec: float = field(default_factory=lambda: _get_float("REQUEST_TIMEOUT_SEC", 30.0))

    # --- Мониторинг ---
    # Базовый интервал проверки фида, минут. Держим небольшим: карточка должна
    # приходить почти сразу после появления вакансии.
    poll_minutes: float = field(default_factory=lambda: _get_float("POLL_MINUTES", 5.0))
    # Быстрая повторная проверка после прохода, в котором было новое (сек):
    # так «пачка» вакансий уходит подряд, а не ждёт следующего цикла.
    fast_poll_sec: float = field(default_factory=lambda: _get_float("FAST_POLL_SEC", 120.0))
    max_per_poll: int = field(default_factory=lambda: _get_int("MAX_PER_POLL", 8))
    # Сколько страниц каждого фида просматривать за один цикл
    feed_pages: int = field(default_factory=lambda: _get_int("FEED_PAGES", 1))

    # --- Отправка ---
    # 1 = пробовать Rich Messages (Bot API 10.1+, красиво), с автооткатом на обычный HTML
    use_rich: bool = field(default_factory=lambda: _get_bool("USE_RICH", True))
    # 1 = разрешить HTML-виджеты (Bot API 10.4+, Telegram 13.2+) для подписчиков,
    # выбравших «Пузырь» в /settings; при сбое — откат на rich/классику
    use_widget: bool = field(default_factory=lambda: _get_bool("USE_WIDGET", True))
    description_limit: int = field(default_factory=lambda: _get_int("DESCRIPTION_LIMIT", 1800))
    # Пауза между отправками сообщений (flood-контроль), сек
    send_delay_sec: float = field(default_factory=lambda: _get_float("SEND_DELAY_SEC", 1.2))

    # --- Служебное ---
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
        """Активные разделы мониторинга (парсим HIRIFY_FEEDS на лету)."""
        return parse_feeds(self.hirify_feeds)

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
