"""Парсинг hirify.me: ссылки вакансий из фида, данные и TG-контакты со страницы вакансии.

Стратегия надёжности: сайт мы заранее знать не можем, поэтому
- ссылки вакансий и Telegram-контакты ищем регекспами по сырому HTML
  (устойчиво к смене CSS-классов и фреймворка);
- основные данные берём из JSON-LD (schema.org JobPosting) — его SEO-сайты
  почти всегда отдают; при его отсутствии — мягкие фолбэки на <h1>/<title>.
"""
from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

# ------------------------------------------------------------------ модели

@dataclass
class TGContact:
    """Один Telegram-контакт из вакансии."""
    kind: str          # 'username' | 'user_id' | 'channel'
    value: str         # username / числовой id / путь канала
    display: str      # как показать пользователю
    url: str          # рабочая ссылка (https://t.me/... или tg://user?id=)
    source: str = "page"   # где найден: contacts|body|mention

    @property
    def is_personal(self) -> bool:
        return self.kind in ("username", "user_id")


@dataclass
class Vacancy:
    job_id: int
    slug: str
    url: str
    title: str = ""
    company: str = ""
    salary: str = ""
    work_format: str = ""
    employment_type: str = ""
    grade: str = ""
    location: str = ""
    skills: list[str] = field(default_factory=list)
    description: str = ""
    date_posted: str = ""
    contacts: list[TGContact] = field(default_factory=list)
    source_type: str = ""          # 'telegram' | 'career' | ''
    contacts_hidden: bool = False  # на странице написано «Show contacts»/«после авторизации»
    feed_key: str = ""             # из какого раздела пришла: remote/global/crypto…
    feed_label: str = ""           # метка раздела для карточки («🌍 Глобальная удалёнка»)
    # КНОПОЧНЫЙ режим (CONTACTS_MODE=button): контакт анонимно не виден —
    # карточка ушла сразу, а кнопка «Показать контакты» сделает 1 auth-запрос
    # по нажатию человека (живой интерес вместо пачки авто-просмотров)
    contacts_by_button: bool = False

    @property
    def has_direct_contact(self) -> bool:
        """Есть ли способ связаться: личный юзернейм/айди ИЛИ пост-контакт в канале.

        Для TG-вакансий hirify «прямым контактом» часто выступает пост в канале
        рекрутера (там лежит ссылка на него) — это тоже рабочий канал связи.
        """
        return any(c.is_personal or c.kind == "channel" for c in self.contacts)

    @property
    def is_crypto(self) -> bool:
        """Есть ли явные крипто/блокчейн-признаки (заголовок, навыки, описание)."""
        return bool(CRYPTO_RE.search(self._search_blob()))

    @property
    def is_target_industry(self) -> bool:
        """Целевая ли индустрия: iGaming / гемблинг / беттинг / дейтинг /
        high-risk-проекты / NDA-проекты / крипта — или смежные.

        Индикаторы ищем там же, где крипто-признаки: заголовок + навыки +
        компания + первые 1500 знаков описания.
        """
        blob = self._search_blob()
        return bool(INDUSTRY_RE.search(blob) or CRYPTO_RE.search(blob))

    def _search_blob(self) -> str:
        """Сводный текст вакансии для индустриальных регекспов."""
        return " ".join(
            [self.title or ""]
            + list(self.skills or [])
            + [self.company or ""]
            + [(self.description or "")[:1500]]
        )

    @property
    def is_non_remote(self) -> bool:
        """True только при ЯВНОМ признаке офиса/гибрида (для строгого фильтра удалёнки)."""
        return bool(NON_REMOTE_RE.search(self.work_format or ""))

    @property
    def primary_contact(self) -> Optional[TGContact]:
        """Основной контакт: сначала личные (юзернейм/айди), затем пост в канале.

        Приоритет внутри типа: найденный в блоке контактов → ссылка → упоминание.
        """
        def _pick(pool: list[TGContact]) -> Optional[TGContact]:
            for pref in ("contacts", "body", "mention"):
                for c in pool:
                    if c.source == pref:
                        return c
            return pool[0] if pool else None

        personal = [c for c in self.contacts if c.is_personal]
        if personal:
            return _pick(personal)
        channels = [c for c in self.contacts if c.kind == "channel"]
        return _pick(channels)


# ------------------------------------------------------------------ регекспы

# Ссылки на вакансии в фиде: /jobs/1261754-kotlin-parser-developer
JOB_LINK_RE = re.compile(r"""(?:href=["']|url["']?\s*[:=]\s*["'])?(?:https?://hirify\.me)?/jobs/(\d+)-([a-z0-9\-_%]+)""", re.IGNORECASE)

# t.me/<username> — личный юзернейм
TELEGRAM_LINK_RE = re.compile(r"""(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z][a-zA-Z0-9_]{2,31})(/[^"'\s<>]*)?""", re.IGNORECASE)
# tg://resolve?domain=<username>
TG_RESOLVE_RE = re.compile(r"""tg://resolve\?domain=([a-zA-Z][a-zA-Z0-9_]{2,31})""", re.IGNORECASE)
# tg://user?id=123
TG_USER_RE = re.compile(r"""tg://user\?id=(\d{4,12})""")
# @username в тексте
MENTION_RE = re.compile(r"""(?<![\w@])@([a-zA-Z][a-zA-Z0-9_]{3,31})\b""")

# Служебные пути t.me — это не личные контакты
TG_SERVICE_PATHS = {
    "s", "share", "joinchat", "login", "addstickers", "addtheme", "addemoji",
    "proxy", "socks", "setlanguage", "premium", "giftcode", "boost", "deep",
    "iv", "faq", "apps", "addlist", "createnewtopic", "map", "wallet",
}
# Имена, похожие на служебные слова/аннотации кода — вырезаем, чтобы не ловить мусор
MENTION_BLOCKLIST = {
    "media", "keyframes", "import", "export", "param", "params", "return",
    "deprecated", "override", "function", "interface", "extends", "implements",
    "suppresswarnings", "property", "setter", "getter", "example", "see",
    "since", "author", "version", "link", "exception", "throws", "test",
    "usage", "dataclass", "instance", "staticmethod", "classmethod", "component",
    "service", "repository", "entity", "wrapper", "builder", "handler",
    "inject", "autowired", "component_scan", "configuration", "controller",
    "everyone", "here", "channel", "admin", "support", "moderator",
}
# сервисные ссылки самого hirify (промо-каналы и боты) — не рекрутеры;
# @hirify_support_bot встречается на каждой странице вакансии (подвал «Help»)
HIRIFY_USERNAMES = {
    "hirify", "hirify_jobs", "hirifybot", "hirify_support_bot",
    "hirify_support", "hirify_global", "hirifyme_bot", "hirify_me",
}

# Крипто-индустрия: заголовок/навыки/описание/компания.
# Латиница — слова с жёсткой границей, кириллица — стемы с продолжением
# («криптовалю» + «тном»), но без ложных срабатываний («криптография», «define»).
CRYPTO_RE = re.compile(
    r"(?i)(?<![\w])(?:"
    # --- латиница ---
    r"crypto(?:currency|currencies)?\b"       # crypto / cryptocurrency / crypto-
    r"|block\s?chain\w*|web\s?3(?:\.0)?\b"
    r"|defi\b|dapp\w*|nft\w*|dao\b"
    r"|solidity|smart\s?contracts?\b|tokenomics?\b"
    r"|on-?chain\b|off-?chain\b"
    r"|zk[-\s]?(?:rollups?|proofs?|sync)\b"
    r"|evm\b|solana\b|polkadot\b|near\s+protocol\b|tron\b"
    r"|binance|bybit|okx\b|coinbase|kraken|kucoin|bitget|gate\.io|hyperliquid"
    r"|usdt\b|usdc\b|tether\b|stablecoins?\b"
    # --- кириллица: стемы, у которых после могут быть буквы/дефис ---
    r"|блокчейн[\w-]*|криптовалю[\w-]*|крипто-?(?:бирж[\w-]*|трейдинг[\w-]*|актив[\w-]*)"
    r"|криптокошел[\w-]*|криптоэкосистем[\w-]*"
    r"|майнинг[\w-]*|смарт-?контракт[\w-]*|токеномик[\w-]*|дефи(?:-[\w-]*)?(?![а-яё])|нфт[\w-]*"
    r"|цифровы[ея] активы?"
    r")"
)

# Целевые индустрии «высокого риска»: iGaming, gambling, betting, дейтинг,
# high-risk-проекты, NDA-проекты и смежные (крипта — отдельный CRYPTO_RE).
# Намеренно узкие шаблоны: plain «gaming» (геймдев), «слот времени»,
# «знакомство с командой» и «процентные ставки» НЕ должны матчиться.
INDUSTRY_RE = re.compile(
    r"(?i)(?<![\w])(?:"
    # --- латиница: iGaming / gambling / betting ---
    r"i[-\s]?gaming\b|gambl\w*"
    r"|bet(?:ting|s)?\b|bookmak\w*|sports?[-\s]?book\w*"
    r"|casino\w*|poker\b|pokies\b|slots\b|slot\s+machin\w*"
    r"|roulette\b|baccarat\b|blackjack\b|craps\b|keno\b|bingo\b"
    r"|lotter(?:y|ies)\b|lotto\b|sweepstak\w*"
    r"|fantasy\s+sport\w*"
    # --- High Risk / NDA ---
    r"|high[-\s]?risk\w*"
    r"|nda\b"
    # --- Dating ---
    r"|dating\w*"
    # --- кириллица ---
    r"|казин\w*|г[еэ]мблинг\w*|букмекер\w*|тотализатор\w*"
    r"|ставки\s+на\s+спорт|спортивн\w*\s+ставк\w*|при[её]м\w*\s+ставок"
    r"|покер\w*|рулетк\w*|слот(?:ы|ов|ах|ами)\b|слот[-\s]?машин\w*|лотере\w*|лото\b"
    r"|высок(?:ий|ого|ая|ой|их|им)[-\s]?риск\w*|высокориск\w*"
    r"|дейт\w*|знакомств\w*"
    r")"
)

# Явно НЕ удалёнка (только для strict-фильтра: положительные признаки офиса/гибрида)
NON_REMOTE_RE = re.compile(
    r"(?i)\b(?:hybrid|onsite|on[-\s]?site|(?:in[-\s]?)?office|гибрид\w*|офис(?:е|а|у|ом|ах|ми)?)\b"
)

# Канонизация формата работы по тексту страницы (первое совпадение выигрывает)
FORMAT_TOKENS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\bglobal\s+remote\b"), "Global Remote"),
    (re.compile(r"(?i)\bremote\b"), "Remote"),
    (re.compile(r"(?i)\b(удал[её]нн\w*|дистанционн\w*)"), "Remote"),
    (re.compile(r"(?i)\bhybrid\b"), "Hybrid"),
    (re.compile(r"(?i)\bгибрид\w*"), "Hybrid"),
    (re.compile(r"(?i)\b(?:onsite|on-site|in-office)\b"), "Onsite"),
    (re.compile(r"(?i)(?<![\wа-яё])офис(?:е|а|у|ом|ах|ми)?(?![\wа-яё])"), "Onsite"),
]


def detect_work_format(page_text: str) -> str:
    """Канонический формат работы по «шапке» страницы (чипы рядом с заголовком)."""
    head = (page_text or "")[:2500]
    for rx, canonical in FORMAT_TOKENS:
        if rx.search(head):
            return canonical
    return ""


def _clean_username(u: str) -> str:
    return u.strip().lstrip("@")


# ------------------------------------------------------------------ фид

def extract_job_links(html: str, base_url: str = "https://hirify.me") -> list[tuple[int, str, str]]:
    """Возвращает [(job_id, slug, url)] в порядке появления на странице (без дублей)."""
    seen: dict[int, tuple[int, str, str]] = {}
    for m in JOB_LINK_RE.finditer(html):
        job_id = int(m.group(1))
        slug = m.group(2)
        if job_id in seen:
            continue
        # обрезаем хвосты вроде кавычек/скобок в regex-контексте
        slug = slug.rstrip("-").strip("\"'")
        seen[job_id] = (job_id, slug, f"{base_url}/jobs/{job_id}-{slug}")
    return list(seen.values())


# ------------------------------------------------------------------ контакты

def _collect_link_contacts(raw_html: str, text_hint: str) -> list[TGContact]:
    """Собираем t.me/tg:// контакты по сырому HTML (href-ы и текст)."""
    contacts: list[TGContact] = []
    seen: set[str] = set()

    def _add(c: TGContact) -> None:
        key = f"{c.kind}:{c.value.lower()}"
        if key in seen:
            return
        seen.add(key)
        contacts.append(c)

    for m in TG_RESOLVE_RE.finditer(raw_html):
        u = _clean_username(m.group(1))
        if u.lower() in MENTION_BLOCKLIST or u.lower() in HIRIFY_USERNAMES:
            continue
        _add(TGContact("username", u, f"@{u}", f"https://t.me/{u}", "body"))

    for m in TG_USER_RE.finditer(raw_html):
        uid = m.group(1)
        _add(TGContact("user_id", uid, f"tg://user?id={uid}", f"tg://user?id={uid}", "body"))

    for m in TELEGRAM_LINK_RE.finditer(raw_html):
        username = _clean_username(m.group(1))
        rest = (m.group(2) or "").strip()
        low = username.lower()
        if low in TG_SERVICE_PATHS or low in HIRIFY_USERNAMES:
            continue
        if low in MENTION_BLOCKLIST:
            continue
        if username.startswith("+"):
            continue  # инвайт-ссылки каналов — отдельно
        if rest:
            # t.me/channel/12345 — пост в канале: полезно как источник, но не личный контакт
            _add(TGContact("channel", username + rest, f"{username}{rest}",
                           f"https://t.me/{username}{rest}", "body"))
            continue
        _add(TGContact("username", username, f"@{username}", f"https://t.me/{username}", "body"))

    return contacts


def _collect_mentions(soup: BeautifulSoup) -> list[TGContact]:
    """@username из видимого текста страницы (без script/style/pre/code)."""
    contacts: list[TGContact] = []
    seen: set[str] = set()
    body = soup.body or soup
    for tag in body.find_all(["script", "style", "pre", "code", "noscript", "svg"]):
        tag.decompose()
    text = body.get_text(" ", strip=True)
    for m in MENTION_RE.finditer(text):
        u = _clean_username(m.group(1))
        low = u.lower()
        if low in MENTION_BLOCKLIST or low in HIRIFY_USERNAMES or low in TG_SERVICE_PATHS:
            continue
        if u in seen:
            continue
        seen.add(u)
        contacts.append(TGContact("username", u, f"@{u}", f"https://t.me/{u}", "mention"))
    return contacts


def _contacts_section_contacts(soup: BeautifulSoup) -> list[TGContact]:
    """Контакты из блока, который сайт показывает как «контакты» (class/id содержит contact)."""
    nodes = []
    for el in soup.find_all(True):
        cls = " ".join(el.get("class", []) or []) if isinstance(el.get("class"), list) else str(el.get("class") or "")
        ident = str(el.get("id") or "")
        blob = (cls + " " + ident).lower()
        if "contact" in blob:
            nodes.append(el)
    if not nodes:
        return []
    html_blob = " ".join(str(n) for n in nodes)
    result: list[TGContact] = []
    for c in _collect_link_contacts(html_blob, ""):
        c.source = "contacts"
        result.append(c)
    return result


# ------------------------------------------------------------------ JSON-LD

def _json_ld(soup: BeautifulSoup) -> list[dict]:
    out = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            out.append(item)
            for sub in item.get("@graph", []) or []:
                if isinstance(sub, dict):
                    out.append(sub)
    return out


def _find_jobposting(lds: list[dict]) -> Optional[dict]:
    for d in lds:
        t = d.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x == "JobPosting" or (isinstance(x, str) and "JobPosting" in x) for x in types if x):
            return d
    return None


def _salary_from_ld(ld: dict) -> str:
    bs = ld.get("baseSalary")
    if not bs or not isinstance(bs, dict):
        return ""
    cur = bs.get("currency", "")
    val = bs.get("value", {})
    if isinstance(val, dict):
        lo, hi = val.get("minValue"), val.get("maxValue")
        unit = val.get("unitText", "")
    else:
        lo, hi, unit = val, None, ""
    try:
        lo_i = int(float(lo)) if lo is not None else None
        hi_i = int(float(hi)) if hi is not None else None
    except (TypeError, ValueError):
        return ""
    fmt = lambda n: f"{n:,}".replace(",", " ")  # noqa: E731
    if lo_i and hi_i:
        s = f"{fmt(lo_i)} – {fmt(hi_i)}"
    elif lo_i:
        s = f"от {fmt(lo_i)}"
    elif hi_i:
        s = f"до {fmt(hi_i)}"
    else:
        return ""
    unit_map = {"MONTH": "/мес", "YEAR": "/год", "HOUR": "/час", "WEEK": "/нед", "DAY": "/день"}
    suffix = (f" {cur}".rstrip() if cur else "") + unit_map.get(str(unit).upper(), "")
    s = (s + suffix).strip()
    return s


def _clean_description(html_or_text: str) -> str:
    """Приводим описание к читаемому тексту (с простыми переносами)."""
    if not html_or_text:
        return ""
    soup = BeautifulSoup(html_or_text.replace("</p>", "</p>\n").replace("<br/>", "\n").replace("<br>", "\n"), "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln:
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1:
                out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _first(*vals, default: str = "") -> str:
    for v in vals:
        if v:
            v = str(v).strip()
            if v:
                return v
    return default


# ------------------------------------------------------------------ главная

def parse_job_page(html: str, url: str) -> Vacancy:
    """Разбор страницы вакансии /jobs/<id>-<slug>."""
    m = re.search(r"/jobs/(\d+)-([a-z0-9\-_%]+)", url, re.IGNORECASE)
    if not m:
        raise ValueError(f"URL не похож на вакансию hirify: {url}")
    job_id, slug = int(m.group(1)), m.group(2)
    v = Vacancy(job_id=job_id, slug=slug, url=url)

    soup = BeautifulSoup(html, "html.parser")

    # --- JSON-LD ---
    lds = _json_ld(soup)
    ld = _find_jobposting(lds)
    if ld:
        v.title = _first(ld.get("title"))
        v.company = _first((ld.get("hiringOrganization") or {}).get("name"))
        v.salary = _salary_from_ld(ld)
        v.description = _clean_description(ld.get("description", "") or "")
        v.date_posted = _first(ld.get("datePosted"))
        v.employment_type = _first(ld.get("employmentType"))
        loc = ld.get("jobLocation") or []
        if isinstance(loc, dict):
            loc = [loc]
        if isinstance(loc, list) and loc:
            addr = (loc[0] or {}).get("address") or {}
            v.location = _first(addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry"))
        if str(ld.get("jobLocationType", "")).upper() == "TELECOMMUTE":
            v.work_format = "Remote"
        region = ld.get("applicantLocationRequirements")
        if isinstance(region, dict):
            country = region.get("name") or (region.get("address") or {}).get("name")
            if country:
                v.work_format = (v.work_format + f" ({country})").strip(" ()")

    # --- фолбэки ---
    if not v.title:
        h1 = soup.find("h1")
        if h1:
            v.title = h1.get_text(" ", strip=True)
    if not v.title:
        t = soup.find("title")
        if t:
            v.title = t.get_text(" ", strip=True).split("—")[0].split("|")[0].strip()
    if not v.company and v.title and "|" in str(soup.title.string if soup.title else ""):
        # «Вакансия — Компания — Hirify»
        parts = [p.strip() for p in soup.title.string.split("|")[0].split("—")]
        if len(parts) >= 2:
            v.company = parts[1]

    if not v.salary:
        # простая эвристика: суммы с валютой в первых ~2500 символах видимого текста
        head = soup.get_text(" ", strip=True)[:2500]
        m2 = re.search(
            r"(\d[\d\s,.]{2,12})\s*([–—-])\s*(\d[\d\s,.]{2,12})\s*([€$₽]|руб|rub|usd|eur)",
            head, re.IGNORECASE)
        if m2:
            v.salary = f"{m2.group(1).strip()} – {m2.group(3).strip()} {m2.group(4)}"

    # грейд — по тексту страницы (короткие сигналы)
    grades = ["C-level", "Director", "Head", "Lead", "Senior", "Middle", "Junior", "Trainee"]
    grade_hits = [g for g in grades if re.search(rf"(?i)\b{re.escape(g)}\b", soup.get_text(" ", strip=True)[:3000])]
    if grade_hits:
        v.grade = grade_hits[0]

    # --- тип источника и «скрытые контакты» ---
    page_text = soup.get_text(" ", strip=True)

    # формат работы: если JSON-LD не дал («Remote») — смотрим чипы в шапке страницы
    if not v.work_format:
        v.work_format = detect_work_format(page_text)
    if re.search(r"(?i)source\s*[-–—]\s*telegram", page_text):
        v.source_type = "telegram"
    v.contacts_hidden = bool(
        re.search(r"(?i)(available after authorization|show contacts|после авторизации)", page_text)
        or TELEGRAM_LINK_RE.search(html) is None and "telegram" in page_text.lower()
    )

    # --- навыки: из JSON-LD или из тегов-«чипов» ---
    if ld and ld.get("skills"):
        if isinstance(ld["skills"], str):
            v.skills = [s.strip() for s in re.split(r"[,;]", ld["skills"]) if s.strip()][:12]
    if not v.skills:
        chips: list[str] = []
        seen: set[str] = set()
        for a in soup.select("a[href*='skills='], [class*='tag'], [class*='chip']")[:40]:
            txt = a.get_text(" ", strip=True)
            key = txt.lower()
            if txt and 1 < len(txt) < 30 and key not in seen:
                seen.add(key)
                chips.append(txt)
        v.skills = chips[:12]

    # --- КОНТАКТЫ: самое важное ---
    # 1) в «контактном» блоке (если сайт отдаёт его в HTML)
    sec = _contacts_section_contacts(soup)
    # 2) все t.me / tg:// ссылки со страницы
    links = _collect_link_contacts(html, page_text)
    # 3) @упоминания в тексте
    mentions = _collect_mentions(soup)
    # 4) из ПОЛНОГО описания (JSON-LD SEO): видимая страница прячет контакты
    #    за «Show contacts», но SEO-разметка несёт текст целиком — блок
    #    «Контакты: Telegram @handle» доезжает анонимно, без API и cookie
    desc_contacts: list[TGContact] = []
    if v.description:
        desc_contacts = _collect_link_contacts(v.description, "")
        for m in MENTION_RE.finditer(v.description):
            u = _clean_username(m.group(1))
            low = u.lower()
            if low in MENTION_BLOCKLIST or low in HIRIFY_USERNAMES or low in TG_SERVICE_PATHS:
                continue
            desc_contacts.append(
                TGContact("username", u, f"@{u}", f"https://t.me/{u}", "mention"))

    merged: list[TGContact] = []
    seen: set[str] = set()
    for c in sec + links + mentions + desc_contacts:
        key = f"{c.kind}:{c.value.lower()}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(c)
    v.contacts = merged

    return v


# ------------------------------------------------------------------ API hirify.me
# Основные данные вакансии бот теперь берёт из официального JSON-API
# (api.hirify.me/api/vacancies/{slug}) — это надёжнее парсинга HTML: страница
# hirify.me рендерится клиентским JS, а API отдаёт всё разом.

SALARY_UNITS = {
    "month": "/мес", "MONTH": "/мес",
    "year": "/год", "YEAR": "/год",
    "hour": "/час", "HOUR": "/час",
    "week": "/нед", "WEEK": "/нед",
    "day": "/день", "DAY": "/день",
}


# --------------------------------------------------------------- сериализация

def vacancy_to_payload(v: Vacancy) -> str:
    """Полный JSON Vacancy — для очереди модерации публикаций.

    Рендер карточки в канал происходит ПОСЛЕ решения владельца (кнопка), а
    раны Actions самозамещаются: между доставкой и нажатием был рестарт,
    поэтому полное состояние карточки (описание, стек, контакты, даты)
    обязано жить в БД, а не в памяти.
    """
    d = {
        "job_id": v.job_id, "slug": v.slug, "url": v.url,
        "title": v.title, "company": v.company, "salary": v.salary,
        "work_format": v.work_format, "employment_type": v.employment_type,
        "grade": v.grade, "location": v.location, "skills": list(v.skills or []),
        "description": v.description, "date_posted": v.date_posted,
        "source_type": v.source_type, "contacts_hidden": v.contacts_hidden,
        "feed_key": v.feed_key, "feed_label": v.feed_label,
        "contacts_by_button": v.contacts_by_button,
        "contacts": [
            {"kind": c.kind, "value": c.value, "display": c.display,
             "url": c.url, "source": c.source} for c in v.contacts
        ],
    }
    return json.dumps(d, ensure_ascii=False)


# ---------------------------------------------------------------- дедуп контента

_NONWORD_RE = re.compile(r"[\W_]+", re.UNICODE)


def _norm_text(s: str) -> str:
    """Нормализация для дедупа: нижний регистр, ё→е, всё не-буквоцифровое —
    в пробел (эмодзи, пунктуация, тире), пробелы схлопнуты, хвост до 60."""
    s = (s or "").lower().replace("ё", "е")
    s = _NONWORD_RE.sub(" ", s)
    return " ".join(s.split())[:60]


def dedup_hash(v: Vacancy) -> str:
    """Отпечаток СОДЕРЖИМОГО вакансии — дедуп между источниками.

    Один и тот же пост HR раскидывает по нескольким каналам: job_id у копий
    разный (id поста в каждом канале свой), поэтому дедуп по id их не видит,
    а дубли в канале владельца — потеря доверия. Ключ: нормализованные
    заголовок + компания (у репостов они совпадают); нет компании — заголовок
    + первый контакт (один и тот же рекрутер). Пустой заголовок — «» (дедуп
    не срабатывает, надёжнее промолчать, чем съесть уникальные вакансии).
    """
    title = _norm_text(v.title)
    if not title:
        return ""
    company = _norm_text(v.company)
    if company:
        key = f"{title}|{company}"
    else:
        contact = next((c.value for c in v.contacts if c.value), "")
        key = f"{title}|{_norm_text(contact)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def vacancy_from_payload(raw: str | None) -> Optional[Vacancy]:
    """Обратное к vacancy_to_payload; мусор/пусто → None (не поднимаем)."""
    try:
        d = json.loads(raw or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or not d.get("job_id"):
        return None  # пусто/битое — карточки нет
    contacts = []
    for c in d.get("contacts") or []:
        if isinstance(c, dict):
            contacts.append(TGContact(
                kind=str(c.get("kind") or ""), value=str(c.get("value") or ""),
                display=str(c.get("display") or ""), url=str(c.get("url") or ""),
                source=str(c.get("source") or "page"),
            ))
    raw_skills = d.get("skills")
    skills = [str(s) for s in raw_skills] if isinstance(raw_skills, list) else []
    try:
        return Vacancy(
            job_id=int(d.get("job_id") or 0), slug=str(d.get("slug") or ""),
            url=str(d.get("url") or ""), title=str(d.get("title") or ""),
            company=str(d.get("company") or ""), salary=str(d.get("salary") or ""),
            work_format=str(d.get("work_format") or ""),
            employment_type=str(d.get("employment_type") or ""),
            grade=str(d.get("grade") or ""), location=str(d.get("location") or ""),
            skills=skills,
            description=str(d.get("description") or ""),
            date_posted=str(d.get("date_posted") or ""),
            contacts=contacts, source_type=str(d.get("source_type") or ""),
            contacts_hidden=bool(d.get("contacts_hidden")),
            feed_key=str(d.get("feed_key") or ""),
            feed_label=str(d.get("feed_label") or ""),
            contacts_by_button=bool(d.get("contacts_by_button")),
        )
    except (TypeError, ValueError):
        return None


def _salary_from_api(s) -> str:
    """Зарплата из JSON-API: словарь {min/max/from/to, currency, period/…}."""
    if not isinstance(s, dict):
        return ""
    lo = s.get("min") if s.get("min") is not None else s.get("min_value", s.get("from"))
    hi = s.get("max") if s.get("max") is not None else s.get("max_value", s.get("to"))
    cur = _first(s.get("currency"), s.get("currency_code"))
    unit = _first(s.get("period"), s.get("unit"), s.get("unit_text"))
    try:
        lo_i = int(float(lo)) if lo is not None else None
        hi_i = int(float(hi)) if hi is not None else None
    except (TypeError, ValueError):
        return ""
    fmt = lambda n: f"{n:,}".replace(",", " ")  # noqa: E731
    if lo_i and hi_i:
        out = f"{fmt(lo_i)} – {fmt(hi_i)}"
    elif lo_i:
        out = f"от {fmt(lo_i)}"
    elif hi_i:
        out = f"до {fmt(hi_i)}"
    else:
        return ""
    suffix = (f" {cur}".rstrip() if cur else "") + SALARY_UNITS.get(str(unit), "")
    return (out + suffix).strip()


def _api_tags(data: dict) -> list[str]:
    """Теги-навыки: [{'id':…,'name':'igaming'}, …] или строки."""
    out: list[str] = []
    for t in data.get("tags") or []:
        if isinstance(t, dict):
            name = t.get("name")
            if name:
                out.append(str(name))
        elif isinstance(t, str) and t.strip():
            out.append(t.strip())
    for t in data.get("nice_to_have_tags") or []:
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return out


def _api_contacts(contacts_data: dict) -> list[TGContact]:
    """Контакты из ответа POST /api/vacancies/{slug}/contacts.

    Значения — ссылки t.me: /username (личный) или /channel/123 (пост
    рекрутера в канале — для TG-вакансий это и есть «прямой контакт»).
    """
    out: list[TGContact] = []
    for c in (contacts_data or {}).get("contacts") or []:
        val = str(c.get("value") or "").strip()
        if not val:
            continue
        m = TELEGRAM_LINK_RE.search(val)
        if m:
            username = _clean_username(m.group(1))
            rest = (m.group(2) or "").strip()
            low = username.lower()
            if low in HIRIFY_USERNAMES or low in TG_SERVICE_PATHS or low in MENTION_BLOCKLIST:
                continue
            if rest:
                out.append(TGContact("channel", username + rest, f"@{username}",
                                     f"https://t.me/{username}{rest}", "contacts"))
            else:
                out.append(TGContact("username", username, f"@{username}",
                                     f"https://t.me/{username}", "contacts"))
        elif val.isdigit():
            out.append(TGContact("user_id", val, f"tg://user?id={val}",
                                 f"tg://user?id={val}", "contacts"))
        elif str(c.get("type", "")).lower() == "telegram" and val.lstrip("@"):
            handle = val.lstrip("@")
            out.append(TGContact("username", handle, f"@{handle}",
                                f"https://t.me/{handle}", "contacts"))
    return out


def vacancy_from_feed_item(item: dict, url: str, feed=None) -> Vacancy:
    """Vacancy из элемента АНОНИМНОГО фида — карточка-фолбэк кнопочного режима,
    когда публичная страница не отдалась (нет ни одного запроса аккаунта).

    Полного описания в фиде нет — вместо него tldr; остальные поля фида
    (title/company/salary/стек/формат/регион) — те же, что видит сайт.
    """
    m = re.search(r"/jobs/(\d+)-([a-z0-9\-_%]+)", url, re.IGNORECASE)
    try:
        job_id = int(item.get("id") or (m.group(1) if m else 0) or 0)
    except (TypeError, ValueError):
        job_id = 0
    slug = str(item.get("slug") or (m.group(2) if m else "") or "")
    v = Vacancy(job_id=job_id, slug=slug, url=url)
    v.title = _first(item.get("title"), item.get("original_title"))
    v.company = _first(item.get("company_title"))
    wf = item.get("work_format")
    if isinstance(wf, list):
        v.work_format = ", ".join(x for x in wf if x)
    else:
        v.work_format = _first(wf)
    rt = item.get("remote_type")
    if rt and "remote" in (v.work_format or "").lower():
        v.work_format = f"{v.work_format} · {rt}"
    grades = item.get("grades") or []
    names = [str(g.get("name") or g) for g in grades if isinstance(g, (dict, str))]
    v.grade = _first(*names[:1]) if names else ""
    regions = item.get("regions") or []
    if isinstance(regions, list) and regions and isinstance(regions[0], dict):
        v.location = ", ".join(x for x in (r.get("name") for r in regions if isinstance(r, dict)) if x)
    v.salary = _salary_from_api(item.get("salary"))
    stack: list[str] = []
    # main_stack бывает строкой (а не списком) — раньше итерация по ней
    # давала «скиллы»-одиночные буквы; теперь строка = один навык
    raw_stack = item.get("main_stack")
    if isinstance(raw_stack, str):
        raw_stack = [raw_stack] if raw_stack.strip() else None
    for t in (raw_stack or item.get("tags") or []):
        if isinstance(t, dict):
            name = t.get("name")
            if name:
                stack.append(str(name))
        else:
            stack.append(str(t))
    v.skills = stack[:12]
    v.description = _clean_description(item.get("tldr") or "")
    v.date_posted = str(item.get("created_at") or "")[:10]
    v.source_type = _first(item.get("source"))
    if feed is not None:
        v.feed_key, v.feed_label = feed.key, feed.label
    return v


def vacancy_from_api(data: dict, contacts_data: dict,
                      feed_key: str = "", feed_label: str = "") -> Vacancy:
    """Собирает Vacancy из JSON GET /api/vacancies/{slug} + POST …/contacts."""
    slug = str(data.get("slug") or "")
    try:
        job_id = int(data.get("id") or slug.split("-")[0])
    except (TypeError, ValueError):
        job_id = 0
    v = Vacancy(job_id=job_id, slug=slug, url=f"https://hirify.me/jobs/{slug}")

    v.title = _first(data.get("title"), data.get("original_title"))
    v.company = _first(data.get("company_title"))

    # формат работы: список ['remote'] (+ remote_type: global/…) → строка
    wf = data.get("work_format")
    if isinstance(wf, list):
        v.work_format = ", ".join(x for x in wf if x)
    else:
        v.work_format = _first(wf)
    rt = data.get("remote_type")
    if rt and "remote" in (v.work_format or "").lower():
        v.work_format = f"{v.work_format} · {rt}"

    emp = [data.get("work_type") or ""]
    et = data.get("employee_type")
    if isinstance(et, list):
        emp.extend(x for x in et if x)
    v.employment_type = ", ".join(x for x in emp if x).strip(" ,")

    # грейд: список словарей/строк
    grades = data.get("grades") or []
    names = [str(g.get("name") or g) for g in grades if isinstance(g, (dict, str))]
    v.grade = _first(*names[:1]) if names else ""

    regions = data.get("regions") or []
    if isinstance(regions, list) and regions and isinstance(regions[0], dict):
        v.location = ", ".join(x for x in (r.get("name") for r in regions if isinstance(r, dict)) if x)

    v.salary = _salary_from_api(data.get("salary"))
    v.skills = _api_tags(data)[:14]
    v.description = _clean_description(data.get("text") or "")
    v.date_posted = str(data.get("created_at") or "")[:10]
    v.source_type = _first(data.get("source"))
    v.contacts = _api_contacts(contacts_data)
    v.contacts_hidden = False  # контакты получены авторизованно из API
    v.feed_key, v.feed_label = feed_key, feed_label
    return v
