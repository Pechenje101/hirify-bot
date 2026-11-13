"""Тесты источников-ТГ-каналов: разбор t.me/s, id-пространство, конфиг.

Интеграционные тесты конвейера (пост → детектор → карточка → доставка)
живут в test_monitor.py; здесь — блок channels.py и его парсер HTML.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.channels import channel_job_id, parse_channel_html  # noqa: E402
from app.config import parse_tg_channels  # noqa: E402

# --- фрагмент страницы t.me/s/<name> (структура как у реального веб-превью:
# id поста — data-post на ВНУТРЕННЕМ div, НЕ на wrap — сверено живой пробой) ---
PAGE = """
<html><body>
<div class="tgme_container">
 <div class="tgme_widget_message_wrap js-widget_message_wrap" data-view="abcd">
  <div class="tgme_widget_message js-widget_message text_force_block_render" data-post="cpa_people/1234">
   <div class="tgme_widget_message_bubble">
    <div class="tgme_widget_message_text js-message_text" dir="auto">
      Senior Media Buyer (iGaming)<br/>
      Ставка: <b>3-5k$</b>, казино-траф, betting-офферы.<br/>
      Резюме: <a href="https://t.me/hr_igaming">@hr_igaming</a>
      <img class="emoji" alt="🔥" src="/emoji/1f525.png"> Горячая вакансия!
    </div>
    <div class="tgme_widget_message_footer compact js-message_footer">
     <div class="tgme_widget_message_meta"><div class="tgme_widget_message_footer">
      <time datetime="2026-11-11T09:30:00+00:00">09:30</time></div></div>
    </div>
   </div>
  </div>
 </div>
 <div class="tgme_widget_message_wrap js-widget_message_wrap" data-view="efgh">
  <div class="tgme_widget_message js-widget_message" data-post="cpa_people/1235">
   <div class="tgme_widget_message_bubble">
    <div class="tgme_widget_message_text js-message_text" dir="auto">
     Новости рынка: ставки выросли, подробности
     <a href="https://example.com">на сайте</a>
    </div>
    <time datetime="2026-11-11T10:00:00+00:00">10:00</time>
   </div>
  </div>
 </div>
 <div class="tgme_widget_message_wrap js-widget_message_wrap" data-view="ijkl">
  <div class="tgme_widget_message js-widget_message" data-post="cpa_people/1236">
   <div class="tgme_widget_message_bubble">
   <div class="tgme_widget_message_photo_wrap"></div>
   <!-- пост без текста (фото/видео без подписи) — пропускаем -->
  </div>
 </div>
</div>
</div>
</body></html>
"""


def test_parse_channel_html_posts():
    posts = parse_channel_html(PAGE)
    assert [p["id"] for p in posts] == [1234, 1235]  # безтекстовый 1236 пропущен

    first, second = posts
    assert first["created_at"] == "2026-11-11T09:30:00+00:00"
    assert "@hr_igaming" in first["text"]            # упоминание-якорь → текст
    assert "\n" in first["text"]                      # <br/> → перенос
    assert "🔥" in first["text"]                      # <img alt> → эмодзи
    assert "3-5k$" in first["text"]                   # <b> → снят
    assert "<" not in first["text"].replace("", "")   # тегов в тексте нет
    assert first["text"].startswith("Senior Media Buyer")
    # заголовок-строка сохраняет переносы, но без html-мусора
    assert "js-message" not in first["text"]

    assert second["created_at"].startswith("2026-11-11T10:00")
    assert "на сайте" in second["text"]               # анкор-текст ссылки
    assert "example.com" not in second["text"]        # href не нужен — есть текст


def test_parse_channel_html_degenerate_inputs():
    assert parse_channel_html("") == []
    assert parse_channel_html(None) == []
    assert parse_channel_html("<html><body>шапка без постов</body></html>") == []
    assert parse_channel_html("мусор ∉ html") == []
    # битый html не роняет разбор (HTMLParser терпим к мусору)
    assert parse_channel_html("<div class='tgme_widget_message_wrap' data-post='a/x'>") == []
    # data-post НА WRAP (старая структура) тоже не ломается: id берётся с любого div
    legacy = ('<div class="tgme_widget_message_wrap" data-post="ch/42">'
              '<div class="tgme_widget_message_text">пост</div>'
              '<time datetime="2026-11-11T09:00:00+00:00">09:00</time></div>')
    posts = parse_channel_html(legacy)
    assert posts == [{"id": 42, "created_at": "2026-11-11T09:00:00+00:00", "text": "пост"}]


def test_channel_job_id_is_negative_deterministic_and_separated():
    a = channel_job_id("cpa_people", 1234)
    assert a < 0
    assert channel_job_id("cpa_people", 1234) == a          # детерминирован
    assert channel_job_id("other_channel", 1234) != a      # разные каналы
    assert channel_job_id("cpa_people", 1235) != a         # разные посты
    # пространство: не пересекается с id hirify (>0) и карточками /card (700M+)
    assert a < 0 < 1_400_000 < 700_000_000
    # id поста влезает в 28 бит (маска), хвост канала — в старших разрядах
    assert (channel_job_id("cpa_people", (1 << 28) + 7) == channel_job_id("cpa_people", 7))


def test_parse_tg_channels():
    feeds = parse_tg_channels("cpa_people, @opento_igaming, https://t.me/payments_lenkep, https://t.me/s/IT_iGaming")
    assert [f.channel for f in feeds] == ["cpa_people", "opento_igaming", "payments_lenkep", "IT_iGaming"]
    keys = [f.key for f in feeds]
    assert keys == ["tg_cpa_people", "tg_opento_igaming", "tg_payments_lenkep", "tg_it_igaming"]
    assert all(f.source == "tg" for f in feeds)
    assert all(f.base_url.startswith("https://t.me/") for f in feeds)
    assert feeds[0].label == "📡 Cpa People"  # человекочитаемая подпись
    # дубликаты по ключу (разный регистр имён) не плодят источников
    assert len(parse_tg_channels("cpa_people,CPA_PEOPLE,@cpa_people")) == 1
    # мусор игнорируется, пусто → пусто
    assert parse_tg_channels("") == []
    assert parse_tg_channels("  , ,") == []
    assert parse_tg_channels("не имя") == []


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


# интеграция: см. test_monitor.py (test_channel_*); здесь — только блок channels
VACANCY_POST = (
    "Senior Media Buyer (iGaming)\n"
    "Ставка: 3-5k$, казино-траф, betting-офферы, Facebook Ads.\n"
    "Опыт от 2 лет, работа удалённая.\n"
    "Резюме: @hr_igaming"
)


def test_pipeline_from_channel_post_text():
    """Текст поста канала → детектор пропускает → Vacancy с контактом."""
    from app.textparse import looks_like_vacancy, parse_vacancy_text

    assert looks_like_vacancy(VACANCY_POST)
    v = parse_vacancy_text(
        VACANCY_POST,
        date_posted="2026-11-11",
        feed_key="tg_cpa_people",
        feed_label="📡 Cpa People",
    )
    assert v.has_direct_contact                 # @hr_igaming вытащен
    assert v.feed_key == "tg_cpa_people"
    assert v.date_posted == "2026-11-11"        # дата публикации поста, не «сегодня»
    assert v.is_target_industry                  # casino/betting — целевая индустрия
    assert "3 000 – 5 000" in v.salary or "3-5" in v.salary.lower() or v.salary  # зарплата распознана
    news = "Новости рынка: ставки выросли, подробности на сайте."
    assert not looks_like_vacancy(news)         # болтовня канала не карточка
