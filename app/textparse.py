"""Парсер свободного текста вакансии → Vacancy (команда /card).

Задача: юзер присылает текст вакансии — как угодно отформатированный (скопипащенный
с сайта, из канала, из письма) — а мы превращаем его в объект Vacancy, из которого
существующие рендеры карточек (пузырь / rich / классика) сразу собирают красивый вид.

Что распознаём (жадно, но консервативно — «лучше не найти, чем найти лишнее»):
- заголовок: первая строка (или «Должность: …» / «Position: …»), при разделителях
  « | », « — », « – », « @ » правая часть идёт в компанию;
- зарплата: диапазоны и «от/до», валюты ($ USD USDT USDC ₽ RUB EUR UAH), суффиксы
  k/к/тыс («3-5k$», «от 150к», «100 000 ₽/мес», «$25/час»), период (/мес, /час…);
- формат работы: удалёнка/гибрид/офис (переиспользуем detect_work_format);
- занятость: full-time / part-time / контракт / стажировка;
- грейд: Junior / Middle / Senior / Lead / Principal (латиница и кириллица);
- локация: строки «Локация: …» / «Location: …» / «📍 …»;
- скиллы: словарь ~90 популярных технологий (крипто/web3 + общий IT) + хэштеги;
- Telegram-контакты: @упоминания, t.me/ссылки, tg://user?id=, tg://resolve
  (регекспы и блок-листы переиспользуются из app/parsing.py).

Карточка не попадает в БД мониторинга (jobs) — это разовый рендер, дедуп
не нужен. job_id детерминированно выводится из хэша текста и живёт в диапазоне
700 000 000+, куда id реальных вакансий hirify (сейчас ~1,4 млн) не достают.
"""
from __future__ import annotations

import re
import zlib
from datetime import date

from .parsing import (
    MENTION_BLOCKLIST,
    MENTION_RE,
    TELEGRAM_LINK_RE,
    TG_RESOLVE_RE,
    TG_SERVICE_PATHS,
    TG_USER_RE,
    TGContact,
    Vacancy,
    detect_work_format,
)

# ---------------------------------------------------------------- числa

# Число с разделителями тысяч (3 500 / 3,500 / 3.500) или десятичной дробью (3.5)
_NUM = r"(?:\d{1,3}(?:[.,\u00a0\s]\d{3})+|\d+(?:[.,]\d+)?)"
_CUR = r"(?:usdt|usdc|usd|dollar\w*|uah|грн\w*|руб\w*|rsd|\$|₽|€|£)"

# «3-5k$», «$3000–5000», «от 150к», «до 200к руб», «100 000 ₽/мес», «$25/час»
SALARY_RE = re.compile(
    rf"""(?ix)
    (?:
        (?P<pre_cur>{_CUR})\s*
        (?P<lo1>{_NUM})
        (?:\s*(?:[–—-]|\sдо\s|\sto\s)\s*(?P<hi1>{_NUM}))?
        (?:\s*(?P<k1>к|k|тыс\w*))?
      |
        (?P<from>(?:от|до|up\s+to)\s+)?
        (?P<lo2>{_NUM})
        (?:\s*(?:[–—-]|\sдо\s|\sto\s)\s*(?P<hi2>{_NUM}))?
        (?:\s*(?P<k2>к|k|тыс\w*))?
        (?:\s*(?P<plus>\+))?
        \s*
        (?P<post_cur>{_CUR})?
    )
    (?:\s*(?P<unit>в\s+час|/час\w*|за\s+час|в\s+месяц|/мес\w*|/month\w*|per\s+month|monthly
        |в\s+нед\w*|/нед\w*|per\s+week|weekly|в\s+год|/год\w*|per\s+year
        |/hour\w*|per\s+hour|hourly))?
    """,
)

# Валюты к каноничному виду (как в карточках мониторинга: «4 500 – 6 500 USD/мес»)
_CURRENCY_MAP = {
    "$": "$", "dollar": "$", "dollars": "$",
    "usd": "USD", "usdt": "USDT", "usdc": "USDC",
    "₽": "₽", "rub": "₽", "руб": "₽",
    "€": "EUR", "eur": "EUR",
    "uah": "UAH", "грн": "UAH",
    "rsd": "RSD", "£": "GBP",
}
_UNIT_MAP = {
    "/мес": "/мес", "в месяц": "/мес", "/month": "/мес", "per month": "/мес", "monthly": "/мес",
    "/час": "/час", "в час": "/час", "за час": "/час", "/hour": "/час", "per hour": "/час", "hourly": "/час",
    "/нед": "/нед", "в неделю": "/нед", "в нед": "/нед", "/week": "/нед", "per week": "/нед", "weekly": "/нед",
    "/год": "/год", "в год": "/год", "/year": "/год", "per year": "/год",
}


def _to_number(raw: str) -> float:
    """«3 500»/«3,500»/«3.500» → 3500 (группы по 3 — тысячи); «3.5» → 3.5."""
    s = raw.replace("\u00a0", "").replace(" ", "").strip()
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", s):
        return float(s.replace(",", "").replace(".", ""))
    if "," in s and "." in s:
        s = s.replace(",", "")  # «1,000.00»
    return float(s.replace(",", ".") or 0)


def _fmt(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def _currency_of(cur: str) -> str:
    """Каноничное обозначение: «rub»→₽, «рублей»→₽, «usdt»→USDT (не «USD!»).

    Проверяем по самому длинному совпадающему префиксу, иначе «usdt»
    застревал бы на ключе «usd».
    """
    c = (cur or "").lower().strip()
    if not c:
        return ""
    for key in sorted(_CURRENCY_MAP, key=len, reverse=True):
        if c.startswith(key):
            return _CURRENCY_MAP[key]
    return c.upper()


def parse_salary(text: str) -> str:
    """Первая РАСПОЗНАННАЯ зарплата в тексте, в стиле карточек («3 000 – 5 000 $/мес»).

    Пустая строка = не нашли. Консервативно: без валюты и суффикса k/к не считаем
    числом зарплаты — иначе поймали бы любой год («от 3 лет опыта») или версию.
    Поэтому проходим по ВСЕМ совпадениям, пока не найдём квалифицирующее.
    """
    for m in SALARY_RE.finditer(text):
        g = m.groupdict()
        cur_raw = g.get("pre_cur") or g.get("post_cur") or ""
        k = (g.get("k1") or g.get("k2") or "")
        lo_raw, hi_raw = g.get("lo1") or g.get("lo2"), g.get("hi1") or g.get("hi2")
        if (not lo_raw and not hi_raw) or (not cur_raw and not k):
            continue  # не похоже на зарплату — пробуем следующее совпадение

        def val(raw: str | None) -> float | None:
            if raw is None:
                return None
            n = _to_number(raw)
            if k:
                n *= 1000
            return n

        lo, hi = val(lo_raw), val(hi_raw)
        # «к» без валюты в русском тексте почти всегда рубли, латинское k — доллары
        if not cur_raw and k:
            cur_raw = "₽" if k.lower().startswith("к") or k.lower().startswith("тыс") else "$"

        def unit() -> str:
            u = (g.get("unit") or "").lower().strip()
            for key, canon in _UNIT_MAP.items():
                if u.startswith(key):
                    return canon
            return ""

        cur_s = _currency_of(cur_raw)
        from_kw = (g.get("from") or "").strip().lower()
        if lo and hi:
            return f"{_fmt(lo)} – {_fmt(hi)} {cur_s}{unit()}".replace(" /", "/").strip()
        if from_kw in ("до", "up to"):
            return f"до {_fmt(hi or lo)} {cur_s}{unit()}".replace(" /", "/").strip()
        prefix = "от" if (from_kw or g.get("plus")) else ""
        n = lo if lo is not None else hi
        if n is None:
            continue
        return f"{prefix}{' ' if prefix else ''}{_fmt(n)} {cur_s}{unit()}".replace(" /", "/").strip()
    return ""


# ---------------------------------------------------------------- заголовок

# Явные метки строк «Поля: значение» — вытаскиваем значение, строку убираем из описания
_LABEL_RE = {
    "title": re.compile(r"(?i)^\s*(?:должность|позиция|вакансия|title|position|role)\s*[::]\s*(.+)$"),
    "company": re.compile(r"(?i)^\s*(?:компания|работодатель|company|employer|от\s+компании)\s*[::]\s*(.+)$"),
    "location": re.compile(r"(?i)^\s*(?:📍|🌍|🗺)?\s*(?:локация|город|место|location|city|place)\s*[::]\s*(.+)$"),
}
# «в компанию X» / «от компании X» прямо в тексте
_COMPANY_INLINE_RE = re.compile(r"(?i)(?:в|от)\s+компани[июя]\s+([A-Za-zА-Яа-яЁё0-9&.\- ]{2,40})")
_HEADER_SPLIT_RE = re.compile(r"\s*(?:\||@|—|–|·)\s*")

_TITLE_STOP_RE = re.compile(  # правая часть сплита не похожа на компанию — не берём её
    r"(?i)\d|remote|удал|офис|гибрид|hybrid| onsite|зарплат|salary|удалённ"
)

# Маркдаун-заголовок первой строки: «**Название** остальной текст» / «# Название» —
# жирная часть семантически и есть должность, остальное строкой описания
_MD_TITLE_RE = re.compile(r"^(?:\*{2}(?P<b>[^*]+?)\*{2}|#{1,4}\s+(?P<h>.+?))\s*(?P<rest>.*)$")


def _is_header_like(line: str) -> bool:
    """Похоже ли на заголовок: короткая строка без знаков конца предложения."""
    s = line.strip()
    return 0 < len(s) <= 90 and not re.search(r"[.!?](\s|$)", s[:-1] or s)


def _split_title_company(header: str) -> tuple[str, str]:
    """«Senior Solidity | ChainX Labs» → («Senior Solidity», «ChainX Labs»)."""
    parts = [p.strip() for p in _HEADER_SPLIT_RE.split(header) if p.strip()]
    if len(parts) >= 2 and not _TITLE_STOP_RE.search(parts[1]) and len(parts[1]) <= 40:
        return parts[0], parts[1]
    return header, ""


# ---------------------------------------------------------------- грейд/занятость

_GRADE_TOKENS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\b(?:principal|head\s+of)\b"), "Principal"),
    (re.compile(r"(?i)\blead\b|\b(?:тех)?лид\w*\b"), "Lead"),
    (re.compile(r"(?i)\b(?:senior|sen\b|сень\w*|синь\w*)\b"), "Senior"),
    (re.compile(r"(?i)\b(?:middle|mid\b|мид\w*)\b"), "Middle"),
    (re.compile(r"(?i)\b(?:junior|jun\b|джун\w*|intern|стаж[её]р\w*|trainee)\b"), "Junior"),
]
_EMPLOYMENT_TOKENS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\b(?:full[-\s]?time|полн\w*\s+занят\w*|fulltime)\b"), "Full-time"),
    (re.compile(r"(?i)\b(?:part[-\s]?time|частичн\w*\s+занят\w*|parttime)\b"), "Part-time"),
    (re.compile(r"(?i)\b(?:contract|контракт\w*|подряд)\b"), "Contract"),
    (re.compile(r"(?i)\b(?:internship|стажировка)\b"), "Internship"),
]

# ---------------------------------------------------------------- скиллы

# Словарь распознаваемых технологий: матчим по границе слова, без регистра.
_SKILLS: list[str] = [
    # крипто / web3
    "solidity", "vyper", "rust", "golang", "foundry", "hardhat", "forge",
    "ethers.js", "web3.js", "viem", "evm", "svm", "anchor", "solana", "polygon",
    "arbitrum", "optimism", "base", "defi", "dao", "nft", "dex", "cex",
    "chainlink", "uniswap", "aave", "metamask", "tron", "ton", "near protocol",
    "cosmos", "substrate", "polkadot", "move", "sui", "zk", "zk-rollup",
    "layer 2", "l2", "restaking", "staking", "liquid restaking", "memecoin",
    "trading", "market making", "quant", "smart contracts", "audit", "tokenomics",
    # языки и фреймворки
    "python", "javascript", "typescript", "react", "vue", "next.js", "node.js",
    "nestjs", "express", "fastapi", "django", "flask", "php", "laravel",
    "java", "kotlin", "swift", "flutter", "react native", "c#", "c++", "go",
    # данные и инфра
    "postgres", "postgresql", "mysql", "mongodb", "redis", "kafka", "rabbitmq",
    "grpc", "graphql", "docker", "kubernetes", "terraform", "aws", "gcp",
    "azure", "ci/cd", "linux", "bash", "nginx",
    # прочее
    "devops", "sre", "qa", "automation", "selenium", "playwright", "cypress",
    "ml", "pytorch", "tensorflow", "figma", "css", "html", "unity", "unreal",
    "godot", "gamedev", "analytics", "bi", "tableau", "power bi", "sql",
]
_SKILL_RE = {s: re.compile(rf"(?i)(?<![\w#+]){re.escape(s)}(?![\w#+])") for s in _SKILLS}
_HASHTAG_RE = re.compile(r"(?<!\w)#([a-zA-Zа-яА-ЯёЁ][\w-]{1,30})")


def _extract_skills(text: str, title: str) -> list[str]:
    """Найденные в тексте технологии (в порядке появления) + хэштеги. ≤10 штук."""
    found: list[tuple[int, str]] = []
    for skill, rx in _SKILL_RE.items():
        m = rx.search(title)
        if not m:
            m = rx.search(text)
        if m:
            found.append((m.start(), skill))
    for m in _HASHTAG_RE.finditer(text):
        tag = m.group(1)
        if len(tag) >= 3 and tag.lower() not in {"вакансия", "работа", "job", "hiring"}:
            found.append((m.start(), tag))
    found.sort(key=lambda p: p[0])
    out: list[str] = []
    seen: set[str] = set()
    for _, s in found:
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= 10:
            break
    return out


# ---------------------------------------------------------------- контакты


def _collect_contacts(text: str) -> list[TGContact]:
    """TG-контакты из текста: t.me/ссылки, tg://…, @упоминания.

    Блок-листы те же, что у парсера страниц (служебные пути, кодовые имена,
    промо-каналы hirify). Порядок приоритета: ссылки → упоминания.
    """
    contacts: list[TGContact] = []
    seen: set[str] = set()

    def _add(c: TGContact) -> None:
        key = f"{c.kind}:{c.value.lower()}"
        if key in seen:
            return
        seen.add(key)
        contacts.append(c)

    for m in TG_RESOLVE_RE.finditer(text):
        u = m.group(1).strip().lstrip("@")
        if u.lower() in MENTION_BLOCKLIST:
            continue
        _add(TGContact("username", u, f"@{u}", f"https://t.me/{u}", "body"))

    for m in TG_USER_RE.finditer(text):
        uid = m.group(1)
        _add(TGContact("user_id", uid, f"tg://user?id={uid}", f"tg://user?id={uid}", "body"))

    for m in TELEGRAM_LINK_RE.finditer(text):
        username = m.group(1).strip().lstrip("@")
        rest = (m.group(2) or "").strip()
        low = username.lower()
        if low in TG_SERVICE_PATHS or low in MENTION_BLOCKLIST or username.startswith("+"):
            continue
        if rest:  # t.me/channel/123 — пост в канале
            _add(TGContact("channel", username + rest, f"{username}{rest}",
                           f"https://t.me/{username}{rest}", "body"))
        else:
            _add(TGContact("username", username, f"@{username}", f"https://t.me/{username}", "body"))

    for m in MENTION_RE.finditer(text):
        u = m.group(1).strip().lstrip("@")
        low = u.lower()
        if low in MENTION_BLOCKLIST or low in TG_SERVICE_PATHS or len(u) < 4:
            continue
        _add(TGContact("username", u, f"@{u}", f"https://t.me/{u}", "mention"))

    return contacts


# ---------------------------------------------------------------- сборка


def _clean_lines(text: str) -> list[str]:
    """Построчная чистка: маркдаун-жирность (**…**, __…__) и хвостовые пробелы."""
    out: list[str] = []
    for ln in text.splitlines():
        ln = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", ln)  # *жирный* / **жирный**
        # _курсив_ — только обрамлённый пробелами, иначе съедали бы подчёркивания
        # в @юзернеймах и t.me-ссылках (@chainx_hr → @chainxhr)
        ln = re.sub(r"(?<=\s)_{1,2}(\S(?:[^_]*?\S)?)_{1,2}(?=\s|$)", r"\1", ln)
        ln = ln.rstrip()
        if ln or (out and out[-1] != ""):
            out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return out


def parse_vacancy_text(
    text: str,
    *,
    date_posted: str = "",
    feed_key: str = "manual",
    feed_label: str = "",
) -> Vacancy:
    """Собирает Vacancy из произвольного текста вакансии.

    date_posted/feed_key/feed_label — для источников мониторинга (ТГ-каналы):
    дата публикации поста и метка раздела, чтобы рассылка подписчиков
    вела себя как у вакансий hirify. По умолчанию label пуст: карточка
    из своего текста не подписывается «из вашего текста» — в шапке
    остаётся только компания (решение владельца 13.11).

    Гарантии: никогда не бросает исключение по содержимому; минимум, который
    возвращаем всегда — описание (исходный текст) и title из первой строки.
    """
    lines = _clean_lines(text)
    body = "\n".join(lines)
    title, company, location = "", "", ""
    desc_lines = list(lines)

    # 1) явные метки («Должность: …», «Компания: …», «Локация: …») — строка уходит из описании
    for key, rx in _LABEL_RE.items():
        for i, ln in enumerate(desc_lines):
            m = rx.match(ln)
            if not m:
                continue
            val = m.group(1).strip()[:60]
            if key == "title" and not title:
                title = val
            elif key == "company" and not company:
                company = val
            elif key == "location" and not location:
                location = val
            desc_lines[i] = ""  # consumed
            break

    # 2) маркдаун-заголовок в ИСХОДНОЙ первой строке («**Название** …» / «# Название»):
    #    чистка маркдауна уже сняла звёздочки, поэтому смотрим сырое начало текста
    if not title:
        first_raw = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        m = _MD_TITLE_RE.match(first_raw)
        if m:
            title = (m.group("b") or m.group("h") or "").strip()[:90]
            rest = (m.group("rest") or "").strip()
            # «**Название** в команду» → в описании остаётся «в команду»
            for i, ln in enumerate(desc_lines):
                if ln.strip():
                    desc_lines[i] = rest
                    break

    # 2б) заголовок из первой непустой строки (если ещё нет), сплит на компанию
    if not title and desc_lines:
        for i, ln in enumerate(desc_lines):
            if not ln.strip():
                continue
            if _is_header_like(ln):
                t, c = _split_title_company(ln)
                title, company = t or title, c or company
                desc_lines[i] = ""
            break  # только первая непустая строка — кандидат на заголовок

    # 3) «в компанию X» / «от компании X» внутри текста
    if not company:
        m = _COMPANY_INLINE_RE.search(body)
        if m:
            company = m.group(1).strip().rstrip(",.")[:40]

    if not title:
        # фолбэк: первые слова первой строки — чтобы карточка не была безымянной
        first = next((ln for ln in lines if ln.strip()), "")
        title = first.strip()[:60].rstrip(" ,;:-|") or "Вакансия"

    # 4) зарплата / формат / грейд / занятость / скиллы / контакты
    salary = parse_salary(body)
    work_format = detect_work_format(body)
    grade = ""
    for rx, canon in _GRADE_TOKENS:
        if rx.search(title) or rx.search(body[:200]):
            grade = canon
            break
    employment = ""
    for rx, canon in _EMPLOYMENT_TOKENS:
        if rx.search(body):
            employment = canon
            break
    skills = _extract_skills(body, title)
    contacts = _collect_contacts(body)

    description = "\n".join(desc_lines).strip()
    if not description:  # текст целиком ушёл в поля — карточке нужно хоть что-то
        description = body.strip()

    digest = zlib.crc32(body.encode("utf-8")) & 0xFFFFFFF  # < 268 млн
    return Vacancy(
        job_id=700_000_000 + digest,  # далеко от реальных id hirify (~1,4 млн)
        slug="card-from-text",
        url="",  # источника-страницы нет; рендеры его не требуют
        title=title.strip()[:90],
        company=company.strip()[:40],
        salary=salary,
        work_format=work_format,
        employment_type=employment,
        grade=grade,
        location=location,
        skills=skills,
        description=description,
        date_posted=(date_posted or date.today().isoformat()),  # футер «опубликовано DD.MM.YYYY»
        contacts=contacts,
        feed_key=feed_key,
        feed_label=feed_label,
    )


# ---------------------------------------------------------------- авто-детект

# Текст вакансии отличается от обычной болтовни: должность, деньги, контакт,
# требования. Голая длина — не признак (жалоба бывает длинной), поэтому
# считаем «маяки» и требуем минимум три. Весомые (зарплата, TG-контакт)
# дают по 2: их у живых вакансий почти всегда хватает.
_ROLE_WORDS = re.compile(
    r"(?i)\b(?:"
    r"engineer|developer|manager|designer|analyst|architect|consultant|"
    r"specialist|administrator|coordinator|recruiter|mentor|"
    r"qa|devops|sre|intern|junior|middle|senior|lead|principal|"
    r"инженер|разработчик|менеджер|дизайнер|аналитик|архитектор|специалист|"
    r"администратор|координатор|консультант|наставник|рекрутер|директор|"
    r"руководитель|должность|вакан(?:сия|ции)|position|vacancy|hiring"
    r")\b"
)
_FORMAT_WORDS = re.compile(
    r"(?i)(удал[её]нн|гибрид|офис|remote|hybrid|onsite|on-site|work\s+from\s+anywhere)"
)
_LABEL_WORDS = re.compile(
    r"(?i)(должность|position|компани[ия]|company|зарплата|salary|вилка|зп\b|"
    r"доход|опыт\s|experience|стек|stack|навыки|обязанности|требования|условия|"
    r"оформление|график|занятость)"
)
# Соискатель описывает СЕБЯ, а не вакансию: «ищу работу», «моё резюме» —
# не карточка, а просьба. Срабатывание на такое — главный ложный сигнал.
# ВАЖНО: голое «резюме»/«resume» сюда НЕ входит — в постах каналов-вакансий
# «Резюме: @hr» — это инструкция откликнуться, а не рассказ о себе.
_SEEKER_WORDS = re.compile(
    r"(?i)\b(?:ищу\s+(?:работу|вакансию|проект)|(?:мо[её]|сво[её])\s+резюме|"
    r"резюме\s+(?:в|ниже)|my\s+(?:cv|resume)|"
    r"рассматриваю\s+(?:вакансии|предложения)|open\s+to\s+work)\b"
)
AUTOCARD_MIN_CHARS = 80  # короче 80 символов вакансию не описывают


def looks_like_vacancy(text: str) -> bool:
    """Похоже ли сообщение на описание вакансии — для авто-карточки без команды.

    Консервативно: лучше промолчать на спорном тексте (юзер позовёт /card),
    чем собирать «карточку» из жалобы или привета. Порог — 3 маяка, из них
    зарплата и Telegram-контакт весят по 2.
    """
    if not text:
        return False
    body = text.strip()
    if len(body) < AUTOCARD_MIN_CHARS:
        return False
    if _SEEKER_WORDS.search(body):
        return False
    score = 0
    if parse_salary(body):
        score += 2
    if MENTION_RE.search(body) or TELEGRAM_LINK_RE.search(body):
        score += 2
    if _ROLE_WORDS.search(body):
        score += 1
    if _FORMAT_WORDS.search(body):
        score += 1
    if _LABEL_WORDS.search(body):
        score += 1
    return score >= 3
