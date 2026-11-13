"""Модерация публикаций: очередь в БД, панели, публикация по кнопке.

Ядро без aiogram-диспетчера (как test_contacts_button): очередь+payload в БД,
панель при доставке админу, publish_job из payload, решения.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.monitor import Monitor  # noqa: E402
from app.moderation import (  # noqa: E402
    auto_bind_channel, close_job, delete_post, manage_keyboard,
    moderation_active, moderation_target, panel_keyboard, panel_text,
    publish_job, queue_for_moderation,
)
from app.parsing import vacancy_from_payload, vacancy_to_payload  # noqa: E402
from app.services import Services  # noqa: E402
from app.textparse import parse_vacancy_text  # noqa: E402

ADMIN = 1
CHANNEL = -100500

SAMPLE = (
    "Senior Solidity <Engineer> | ChainX & Co, 4-5k$ global remote, "
    "стек: EVM, Solidity, Foundry. Грейд senior, полная занятость.\n"
    "Обязанности: аудит смарт-контрактов, ревью.\n"
    "Писать: @chainx_hr"
)


class FakeSender:
    def __init__(self, fail_vacancy=False):
        self.sent: list[tuple[int, str]] = []
        self.vacancies: list = []
        self.displays: list[str] = []
        self.texts: list[tuple[int, str, object]] = []
        self.fail_vacancy = fail_vacancy
        # «Bot»: edit/delete для закрытия/удаления постов канала
        class _Bot:
            edited: list = []
            deleted: list = []

            async def edit_message_text(self, chat_id, message_id, text, **kw):
                self.edited.append((chat_id, message_id, text, kw.get("reply_markup")))
                return True

            async def delete_message(self, chat_id, message_id):
                self.deleted.append((chat_id, message_id))
                return True

        self.bot = _Bot()

    async def send_vacancy(self, chat_id, v, *, silent=False, display="auto",
                           want_message=False):
        if self.fail_vacancy:
            raise RuntimeError("bot is not admin")
        self.sent.append((chat_id, v.title))
        self.vacancies.append(v)
        self.displays.append(display)
        return ("classic", 4242) if want_message else "classic"

    async def send_text(self, chat_id, html, markup=None):
        self.texts.append((chat_id, html, markup))


def make_cfg(post_channel_id: int = CHANNEL, admin: int = ADMIN) -> Config:
    cfg = Config(bot_token="x")
    cfg.tg_channels = ""
    cfg.post_channel_id = post_channel_id
    cfg.admin_chat_id = admin
    return cfg


def make_services(post_channel_id: int = CHANNEL, db=None,
                  sender=None) -> Services:
    cfg = make_cfg(post_channel_id)
    db = db or Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    mon = Monitor(object(), db, sender or FakeSender(), cfg)  # client не нужен
    return Services(cfg=cfg, db=db, client=object(), sender=mon.sender, monitor=mon)


def sample_vacancy(job_id: int = 101):
    return parse_vacancy_text(SAMPLE, feed_key="tg_test", feed_label="📡 Test")


# ---------------------------------------------------------------- payload

def test_payload_roundtrip():
    v = sample_vacancy()
    v2 = vacancy_from_payload(vacancy_to_payload(v))
    assert v2 is not None
    assert v2.title == v.title and v2.company == v.company and v2.salary == v.salary
    assert v2.description == v.description
    assert v2.job_id == v.job_id and v2.feed_key == "tg_test"
    assert [c.value for c in v2.contacts] == [c.value for c in v.contacts]
    assert v2.contacts[0].url == v.contacts[0].url
    assert "solidity" in [s.lower() for s in v2.skills] and v2.grade == v.grade


def test_payload_garbage():
    assert vacancy_from_payload("") is None
    assert vacancy_from_payload(None) is None
    assert vacancy_from_payload("не json") is None
    assert vacancy_from_payload("[1,2]") is None
    assert vacancy_from_payload("{}") is None
    assert vacancy_from_payload('{"job_id": "х"}') is None  # битый id — карточки нет
    # битые поля не роняют восстановление — дефолты
    v = vacancy_from_payload('{"job_id": 9, "skills": "не список"}')
    assert v is not None and v.job_id == 9 and v.skills == []


# ---------------------------------------------------------------- БД

def test_db_moderation_columns_and_queue():
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(jobs)")}
    assert {"mod_status", "payload", "dedup_hash", "chan_msg_id", "posted_at"} <= cols

    for jid, fs in ((1, "2026-11-11T10:00:00"), (2, "2026-11-12T10:00:00"),
                    (3, "2026-11-13T10:00:00")):
        db.add_job(jid, f"s{jid}", f"https://t.me/x/{jid}", title=f"Job {jid}")
        db.mark_job(jid, "sent")
        db.conn.execute("UPDATE jobs SET first_seen=? WHERE job_id=?", (fs, jid))
    db.conn.commit()
    db.save_moderation(1, "{}")
    db.save_moderation(2, "{}")
    db.save_moderation(3, "{}")
    db.set_mod(2, "no")
    db.mark_posted(3, 777)
    db.mark_channel_post_gone(3, "closed")

    # FIFO: старые сверху (1 остался в очереди)
    assert [r["job_id"] for r in db.pending_moderation()] == [1]
    st = db.moderation_stats()
    assert st == {"queue": 1, "posted": 0, "closed": 1, "deleted": 0, "rejected": 1}

    # save_moderation пишет payload ровно один раз; mark_posted — время и id поста
    row = db.get_job(1)
    assert row["payload"] == "{}" and row["mod_status"] == "queue"
    closed = db.get_job(3)
    assert closed["posted_at"] != "" and closed["chan_msg_id"] == 0
    db.close()


def test_migration_from_old_db():
    """База до модерации получает колонки при открытии (ALTER TABLE)."""
    import sqlite3
    tmp = tempfile.mkdtemp(prefix="hirify_mod_")
    path = Path(tmp) / "old.db"
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE jobs (job_id INTEGER PRIMARY KEY, slug TEXT,"
                " url TEXT, title TEXT, company TEXT, salary TEXT,"
                " tg_contacts TEXT, attempts INTEGER NOT NULL DEFAULT 0,"
                " first_seen TEXT NOT NULL, sent_at TEXT,"
                " status TEXT NOT NULL DEFAULT 'new')")
    raw.execute("INSERT INTO jobs (job_id, slug, url, title, first_seen)"
                " VALUES (7, 's', 'u', 'Old job', '2026-11-01')")
    raw.commit()
    raw.close()
    db = Database(path)
    row = db.get_job(7)
    assert row["mod_status"] == "" and row["payload"] == ""
    db.close()


# ---------------------------------------------------------------- панель

def test_panel_text_and_keyboard():
    text = panel_text("Senior <Solidity> Engineer", "ChainX & Co", "$4–6k")
    assert "🎛" in text and "&lt;Solidity&gt;" in text and "ChainX &amp; Co" in text
    kb = panel_keyboard(101)
    buttons = [b for row in kb.inline_keyboard for b in row]
    datas = {b.callback_data for b in buttons}
    assert datas == {"mod:ok:101", "mod:no:101"}


# ---------------------------------------------------------------- очередь

def test_queue_on_delivery_sends_panel():
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    cfg = make_cfg()
    sender = FakeSender()
    mon = Monitor(object(), db, sender, cfg)
    db.add_job(101, "s", "https://t.me/x/101", title="Senior Solidity Engineer")
    v = sample_vacancy()
    stats = Monitor._new_stats()
    asyncio.run(mon._deliver_vacancy(
        101, v, [{"chat_id": ADMIN, "silent": False, "display": "auto"}], stats))
    # карточка доставлена + панель под ней
    assert stats["sent"] == 1
    assert sender.sent == [(ADMIN, v.title)]
    assert len(sender.texts) == 1
    chat, html, markup = sender.texts[0]
    assert chat == ADMIN and "Модерация" in html and markup is not None
    job = db.get_job(101)
    assert job["mod_status"] == "queue" and job["status"] == "sent"
    assert vacancy_from_payload(job["payload"]).title == v.title
    db.close()


def test_queue_no_duplicate_panel_on_retry():
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    cfg = make_cfg()
    sender = FakeSender()
    mon = Monitor(object(), db, sender, cfg)
    db.add_job(101, "s", "https://t.me/x/101", title="Job")
    v = sample_vacancy()
    stats = Monitor._new_stats()
    rec = [{"chat_id": ADMIN, "silent": False, "display": "auto"}]
    asyncio.run(mon._deliver_vacancy(101, v, rec, stats))
    asyncio.run(mon._deliver_vacancy(101, v, rec, stats))
    assert len(sender.texts) == 1  # панель одна, вакансия уже в очереди
    db.close()


def test_no_panel_when_admin_not_recipient_or_moderation_off():
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    cfg = make_cfg()
    sender = FakeSender()
    mon = Monitor(object(), db, sender, cfg)
    db.add_job(101, "s", "https://t.me/x/101", title="Job")
    v = sample_vacancy()
    stats = Monitor._new_stats()
    # карточка дошла другому подписчику — панель админу не шлём
    asyncio.run(mon._deliver_vacancy(
        101, v, [{"chat_id": 2, "silent": False, "display": "auto"}], stats))
    assert sender.texts == [] and db.get_job(101)["mod_status"] == ""

    # модерация выключена (канал не задан) — панели нет
    cfg2 = make_cfg(post_channel_id=0)
    sender2 = FakeSender()
    mon2 = Monitor(object(), db, sender2, cfg2)
    db.add_job(102, "s2", "https://t.me/x/102", title="Job 2")
    v2 = sample_vacancy(102)
    stats2 = Monitor._new_stats()
    asyncio.run(mon2._deliver_vacancy(
        102, v2, [{"chat_id": ADMIN, "silent": False, "display": "auto"}], stats2))
    assert sender2.texts == [] and db.get_job(102)["mod_status"] == ""
    db.close()


def test_moderation_target_db_overrides_env():
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    cfg = make_cfg()  # env: -100500
    assert moderation_target(cfg, db) == CHANNEL
    assert moderation_active(cfg, db) is True
    db.set_setting("post_channel_id", "-100999")
    assert moderation_target(cfg, db) == -100999  # БД перекрывает окружение
    db.set_setting("post_channel_id", "")
    assert moderation_target(cfg, db) == CHANNEL
    cfg_off = make_cfg(post_channel_id=0)
    assert moderation_target(cfg_off, db) is None
    assert moderation_active(cfg_off, db) is False
    db.close()


# ---------------------------------------------------------------- публикация

def test_publish_job_ok_and_full_card():
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    sender = FakeSender()
    services = make_services(db=db, sender=sender)
    v = sample_vacancy()
    db.add_job(101, v.slug, v.url, title=v.title)
    db.save_moderation(101, vacancy_to_payload(v))
    out = asyncio.run(publish_job(101, services))
    assert out == "ok"
    # пост ушел в КАНАЛ, карточка полная (описание и контакт из payload)
    assert sender.sent == [(CHANNEL, v.title)]
    sent_v = sender.vacancies[0]
    assert sent_v.description == v.description
    assert sent_v.contacts[0].value == v.contacts[0].value
    # id поста и время записаны (закрыть/удалить/дайджест дня)
    job = db.get_job(101)
    assert job["mod_status"] == "posted" and job["chan_msg_id"] == 4242
    assert job["posted_at"] != ""
    # повторное нажатие — уже решено
    assert asyncio.run(publish_job(101, services)) == "already"
    db.close()


def test_publish_job_display_follows_admin_settings():
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    sender = FakeSender()
    services = make_services(db=db, sender=sender)
    db.update_subscriber_settings(ADMIN, None, False, display="rich")
    v = sample_vacancy(202)
    db.add_job(202, v.slug, v.url, title=v.title)
    db.save_moderation(202, vacancy_to_payload(v))
    asyncio.run(publish_job(202, services))
    assert sender.displays == ["rich"]
    db.close()


def test_publish_job_outcomes():
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    sender = FakeSender()
    services = make_services(db=db, sender=sender)
    # нет канала
    off = make_services(post_channel_id=0, db=db, sender=sender)
    v = sample_vacancy(301)
    db.add_job(301, v.slug, v.url, title=v.title)
    db.save_moderation(301, vacancy_to_payload(v))
    assert asyncio.run(publish_job(301, off)) == "nochan"
    # вакансии нет
    assert asyncio.run(publish_job(999999, services)) == "noid"
    # payload не сохранился
    db.save_moderation(301, "битый json")
    assert asyncio.run(publish_job(301, services)) == "nopayload"
    # сбой отправки: вакансия остаётся в очереди (/pending покажет снова)
    failing = FakeSender(fail_vacancy=True)
    services_fail = make_services(db=db, sender=failing)
    v2 = sample_vacancy(302)
    db.add_job(302, v2.slug, v2.url, title=v2.title)
    db.save_moderation(302, vacancy_to_payload(v2))
    assert asyncio.run(publish_job(302, services_fail)) == "fail"
    assert db.get_job(302)["mod_status"] == "queue"
    db.close()


def test_close_and_delete_post():
    """Закрытие правит пост в канале (замочек, кнопка отклика снята),
    удаление убирает пост; оба статуса выбывают из дайджеста дня."""
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    sender = FakeSender()
    services = make_services(db=db, sender=sender)
    v = sample_vacancy(501)
    db.add_job(501, v.slug, v.url, title=v.title)
    db.save_moderation(501, vacancy_to_payload(v))
    assert asyncio.run(publish_job(501, services)) == "ok"

    # ЗАКРЫТИЕ: бот редактирует свой пост — текст + замочек, кнопки сняты
    assert asyncio.run(close_job(501, services)) == "ok"
    chat, mid, text, markup = sender.bot.edited[0]
    assert chat == CHANNEL and mid == 4242
    assert "Вакансия закрыта" in text and "ChainX" in text and markup is None
    assert db.get_job(501)["mod_status"] == "closed"
    assert asyncio.run(close_job(501, services)) == "already"
    # закрытая не удаляется (панель управления уже снята)
    assert asyncio.run(delete_post(501, services)) == "already"

    # УДАЛЕНИЕ: отдельная вакансия с живым постом
    v2 = sample_vacancy(502)
    db.add_job(502, v2.slug, v2.url, title=v2.title)
    db.save_moderation(502, vacancy_to_payload(v2))
    asyncio.run(publish_job(502, services))
    assert asyncio.run(delete_post(502, services)) == "ok"
    assert sender.bot.deleted == [(CHANNEL, 4242)]
    job = db.get_job(502)
    assert job["mod_status"] == "deleted" and job["chan_msg_id"] == 0
    # из дайджеста дня оба выбывают (дальше — test_reports)
    db.close()


def test_manage_keyboard():
    """Панель управления постом: закрыть / удалить."""
    kb = manage_keyboard(123)
    datas = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert datas == {"mod:close:123", "mod:del:123"}


def test_queue_for_moderation_direct():
    """Ядро очереди без монитора: панели не дублируются, payload пишется."""
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    cfg = make_cfg()
    sender = FakeSender()
    v = sample_vacancy()
    db.add_job(401, v.slug, v.url, title=v.title)
    asyncio.run(queue_for_moderation(401, v, cfg, db, sender, True))
    asyncio.run(queue_for_moderation(401, v, cfg, db, sender, True))
    assert len(sender.texts) == 1
    # админ не получал карточку — в очередь не ставим
    db.add_job(402, "s", "https://t.me/x/402", title="Job 402")
    asyncio.run(queue_for_moderation(402, v, cfg, db, sender, False))
    assert db.get_job(402)["mod_status"] == "" and len(sender.texts) == 1
    db.close()


# ---------------------------------------------------------------- авто-привязка

def test_auto_bind_on_first_channel_post():
    """Бот уже админ: первый пост в канале привязывает его и уведомляет
    владельца; пост в другом канале больше ничего не меняет."""
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    cfg = make_cfg(post_channel_id=0)  # канал не задан
    sender = FakeSender()
    assert asyncio.run(auto_bind_channel(-100777, "My Vacancies", cfg, db, sender)) is True
    assert db.get_setting("post_channel_id") == "-100777"
    assert moderation_target(cfg, db) == -100777
    assert moderation_active(cfg, db) is True
    assert len(sender.texts) == 1
    chat, html, _ = sender.texts[0]
    assert chat == ADMIN and "My Vacancies" in html and "/pending" in html
    # второй канал не подменяет и не спамит уведомлениями
    assert asyncio.run(auto_bind_channel(-100888, "Other", cfg, db, sender)) is False
    assert moderation_target(cfg, db) == -100777
    assert len(sender.texts) == 1
    db.close()


def test_auto_bind_does_not_override_explicit_channel():
    """Явное (/setchannel в БД, POST_CHANNEL_ID в env) важнее авто-привязки."""
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    sender = FakeSender()
    cfg_env = make_cfg(post_channel_id=CHANNEL)  # задан через окружение
    assert asyncio.run(auto_bind_channel(-100222, "X", cfg_env, db, sender)) is False
    assert sender.texts == []
    cfg = make_cfg(post_channel_id=0)
    db.set_setting("post_channel_id", "-100111")  # задан через /setchannel
    assert asyncio.run(auto_bind_channel(-100222, "X", cfg, db, sender)) is False
    assert db.get_setting("post_channel_id") == "-100111"
    assert sender.texts == []
    db.close()


def test_auto_bind_survives_notify_failure_and_works_without_admin():
    """Сбой уведомления не мешает привязке; без ADMIN_CHAT_ID — привязка
    беззвучная (панелей и так не будет до заданного админа)."""
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")

    class BoomSender(FakeSender):
        async def send_text(self, chat_id, html, markup=None):
            raise RuntimeError("fl00d")

    cfg = make_cfg(post_channel_id=0)
    assert asyncio.run(auto_bind_channel(-100333, "Y", cfg, db, BoomSender())) is True
    assert db.get_setting("post_channel_id") == "-100333"
    db.close()

    db2 = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    cfg2 = make_cfg(post_channel_id=0, admin=0)
    sender2 = FakeSender()
    assert asyncio.run(auto_bind_channel(-100444, "Z", cfg2, db2, sender2)) is True
    assert db2.get_setting("post_channel_id") == "-100444"
    assert sender2.texts == []
    db2.close()


def test_channel_post_handler_ignores_own_posts():
    """Хендлер channel_post: собственные публикации (карточки) и посты
    без автора (анонимный админ — from может отсутствовать) не путают
    авто-привязку; пост владельца привязывает канал."""
    from types import SimpleNamespace

    from app.handlers import on_channel_post

    def post(author_id, chat_id=-100777, title="My Vacancies"):
        return SimpleNamespace(
            bot=SimpleNamespace(id=777),
            from_user=SimpleNamespace(id=author_id) if author_id else None,
            chat=SimpleNamespace(id=chat_id, title=title, type="channel"),
        )

    db = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    sender = FakeSender()
    services = make_services(post_channel_id=0, db=db, sender=sender)
    # пост самого бота (карточка из «✅ Опубликовать») — не триггер
    asyncio.run(on_channel_post(post(777), services))
    assert db.get_setting("post_channel_id") == ""
    # пост владельца — привязка + уведомление
    asyncio.run(on_channel_post(post(ADMIN), services))
    assert db.get_setting("post_channel_id") == "-100777"
    assert len(sender.texts) == 1
    db.close()

    db2 = Database(Path(tempfile.mkdtemp(prefix="hirify_mod_")) / "t.db")
    sender2 = FakeSender()
    services2 = make_services(post_channel_id=0, db=db2, sender=sender2)
    # анонимный админ (from_user=None) — тоже привязка
    asyncio.run(on_channel_post(post(None), services2))
    assert db2.get_setting("post_channel_id") == "-100777"
    assert len(sender2.texts) == 1
    db2.close()
