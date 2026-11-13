"""Источник-дополнение: публичные Telegram-каналы с вакансиями.

Зачем: hirify.me — не единственный кладезь; целевые индустрии (iGaming,
CPA/маркетинг, пейменты) плотнее живут в тематических ТГ-каналах. Каналы
публичные, поэтому читаем их БЕЗ Telegram-аккаунта и БЕЗ бота в канале:
страница веб-превью https://t.me/s/<name> отдаёт последние ~20 постов как
обычный HTML — анонимный GET, как фиды hirify (cookie и «просмотры»
аккаунта не тратятся вовсе).

Каждый пост проходит тем же конвейером, что и текст от юзера (/card,
автокарточки): детектор looks_like_vacancy → parse_vacancy_text → Vacancy
→ карточки (Пузырь/Rich/Классика) → рассылка подписчикам раздела канала.

Дедуп: детерминированный ОТРИЦАТЕЛЬНЫЙ job_id из (имя канала, id поста) —
в минус-пространство, куда реальные id вакансий hirify (положительные) не
достают; вакансии из /card живут в 700M+, идём ещё левее.

Свежесть: datetime поста (UTC) ложится в item["created_at"] — общий фильтр
монитора (FRESH_WINDOW_MIN) режет старьё точно так же, как для фида hirify.
"""
from __future__ import annotations

import logging
import zlib
from html.parser import HTMLParser

from .config import Config

log = logging.getLogger("hirify.channels")

HAS_CURL_CFFI = False
try:
    from curl_cffi import CurlAsyncSession  # type: ignore[import-untyped]

    HAS_CURL_CFFI = True
except Exception:  # noqa: BLE001 - ImportError или битая библиотека
    CurlAsyncSession = None  # type: ignore[assignment]

import httpx  # noqa: E402  (после try-импорта: curl_cffi может отсутствовать)

# --- разбор HTML-превью t.me/s/<channel> ---------------------------------------

_CHANNEL_PAGE_URL = "https://t.me/s/{name}"
# маркеры страницы-заглушки t.me/<name> («канал приватный/не существует») —
# только для диагностики в логе, разбор не блокируем: валидная s-страница
# тоже содержит кнопки шапки, и посты важнее догадок по маркерам
_NOT_PUBLIC_MARKERS = ("tgme_page_action", "If you have Telegram")


def channel_job_id(channel: str, message_id: int) -> int:
    """Детерминированный id поста канала: ОТРИЦАТЕЛЬНЫЙ (не пересекается
    с hirify ~1,4M и карточками /card 700M+). crc32(канал)·2^28 + id поста
    (id постов сейчас ~сотни миллионов — в 2^28 влезает с запасом)."""
    h = zlib.crc32(channel.encode("utf-8")) & 0xFFFFFFFF
    return -((h << 28) | (message_id & 0xFFFFFFF))


class _PostExtractor(HTMLParser):
    """Вытаскивает из t.me/s посты: id (data-post="канал/123"), текст
    (div.tgme_widget_message_text) и дату (<time datetime=…>).

    Пост «закрыт» началом следующего (посты — сиблинги), а не </div>:
    внутри wrap вложенные div'ы, точный трекинг глубины лишний.

    Реальная разметка t.me/s (сверено живой пробой с Actions): id поста —
    data-post на ВНУТРЕННЕМ div (tgme_widget_message), а НЕ на wrap —
    там его нет; поэтому id берём с первого data-post внутри открытого поста.

    Особенности разметки, которые учитываем: <br/> → \\n, <img alt="🔥"> →
    эмодзи из alt, <a>текст</a> → текст (юserнеймы Telegram сам оборачивает
    ссылкой — текст и есть @упоминание), сущности (&amp;…) — convert_charrefs.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self._id: int | None = None
        self._dt: str = ""
        self._parts: list[str] = []
        self._capture = False
        self._in_anchor = False
        self._post_open = False

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class") or ""
        if tag == "div" and "tgme_widget_message_wrap" in cls:
            self._flush()
            self._id = None
            self._dt = ""
            self._parts = []
            self._post_open = True
            # старая структура держала data-post прямо на wrap — берём и его
            dp = a.get("data-post") or ""
            if dp:
                mid = dp.partition("/")[2]
                self._id = int(mid) if mid.isdigit() else None
            return
        if not self._post_open:
            return
        data_post = a.get("data-post") or ""
        if tag == "div" and data_post and self._id is None:
            mid = data_post.partition("/")[2]
            self._id = int(mid) if mid.isdigit() else None
        elif tag == "time" and not self._dt:
            self._dt = a.get("datetime") or ""
        elif tag == "div" and "tgme_widget_message_text" in cls:
            self._capture = True
        if self._capture:
            if tag == "br":
                self._parts.append("\n")
            elif tag == "img":
                alt = a.get("alt") or ""
                if alt:
                    self._parts.append(alt)  # эмодзи-картинки: текст = alt
            elif tag == "a":
                self._in_anchor = True

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag == "div":
            self._capture = False
        if tag == "a":
            self._in_anchor = False

    def handle_data(self, data: str) -> None:
        if self._capture and data:
            self._parts.append(data)

    def _flush(self) -> None:
        if self._id is None:
            return
        text = "".join(self._parts).strip()
        if text:
            self.posts.append({"id": self._id, "created_at": self._dt, "text": text})
        self._id = None
        self._post_open = False

    def close(self) -> None:  # последний пост не имеет «следующего»
        self._flush()
        super().close()


def parse_channel_html(html: str) -> list[dict]:
    """HTML страницы t.me/s/<name> → [{id, created_at, text}].

    Пусто/канал не публичный → []. Никогда не бросает исключение.
    """
    if not html:
        return []
    try:
        p = _PostExtractor()
        p.feed(html)
        p.close()
        return p.posts
    except Exception:  # noqa: BLE001 — разбор НИКОГДА не роняет проход
        log.exception("Разбор HTML канала сломался — пропускаю страницу")
        return []


# --- клиент --------------------------------------------------------------------


class ChannelClient:
    """Анонимный читатель t.me/s: curl_cffi (Chrome TLS/HTTP2) с откатом на httpx."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._curl = None
        self._httpx: httpx.AsyncClient | None = None

    async def open(self) -> None:
        if self._curl is not None or self._httpx is not None:
            return
        target = (getattr(self.cfg, "impersonate_target", "") or "chrome").strip()
        if HAS_CURL_CFFI:
            try:
                self._curl = CurlAsyncSession(impersonate=target)  # type: ignore[misc]
                log.info("Каналы: curl_cffi, имперсонация %s", target)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("curl_cffi не поднялся (%s) — каналы читаем httpx", e)
        self._httpx = httpx.AsyncClient(
            timeout=self.cfg.request_timeout_sec, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/131.0.0.0 Safari/537.36"},
        )

    async def close(self) -> None:
        if self._curl is not None:
            await self._curl.close()
            self._curl = None
        if self._httpx is not None:
            await self._httpx.aclose()
            self._httpx = None

    async def fetch_page(self, name: str) -> tuple[str, int, str]:
        """(html, HTTP-статус, финальный URL) страницы веб-превью канала.

        Диагностика редиректов: t.me/s/<имя> непубличного канала уводит на
        заглушку t.me/<имя> («канал не существует»).
        """
        await self.open()
        url = _CHANNEL_PAGE_URL.format(name=name)
        if self._curl is not None:
            r = await self._curl.get(url, headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
            return r.text, r.status_code, str(r.url)
        assert self._httpx is not None
        r = await self._httpx.get(url)
        return r.text, r.status_code, str(r.url)

    async def fetch_posts(self, name: str) -> list[dict]:
        """Последние посты публичного канала (одна страница веб-превью)."""
        html, _status, _url = await self.fetch_page(name)
        posts = parse_channel_html(html)
        if not posts:
            hint = ""
            if any(m in html for m in _NOT_PUBLIC_MARKERS):
                hint = " (канал не публичный/не существует?)"
            elif not html.strip():
                hint = " (пустой ответ)"
            log.warning("Канал %s: постов не прочитано%s", name, hint)
        return posts
