"""Дедуп контента между источниками: копия одной вакансии в разных каналах.

HR раскидывает один пост по нескольким каналам — id копий разные, старый
дедуп по id их не видел. Отпечаток (parsing.dedup_hash): нормализованные
заголовок+компания; нет компании — заголовок+контакт. Копия помечается
skipped_duplicate: до подписчиков и в очередь канала не идёт, в дайджесте
не показывается.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.monitor import Monitor  # noqa: E402
from app.parsing import dedup_hash, vacancy_from_payload, vacancy_to_payload  # noqa: E402
from app.textparse import parse_vacancy_text  # noqa: E402

ADMIN = 1

TEXT_A = (
    "Senior Solidity <Engineer> | ChainX & Co, 4-5k$ global remote, "
    "стек: EVM, Solidity, Foundry. Грейд senior, полная занятость.\n"
    "Обязанности: аудит смарт-контрактов.\n"
    "Писать: @chainx_hr"
)
# РЕПОСТ: тот же текст в другом оформлении (эмодзи, регистр, ё, пунктуация)
TEXT_B = (
    "🔥 Senior Solidity Engineer — CHAINX & CO! 4-5k$ global remote.\n"
    "Стек: EVM, Solidity, Foundry. Грейд senior.\n"
    "Резюме: t.me/chainx_hr"
)


def vac(text: str, job_id: int) -> object:
    return parse_vacancy_text(text, feed_key="tg_test", feed_label="")


def test_same_content_different_ids_is_duplicate():
    a, b = vac(TEXT_A, 101), vac(TEXT_B, 202)
    assert a.job_id != b.job_id
    assert dedup_hash(a) and dedup_hash(a) == dedup_hash(b)


def test_different_company_is_not_duplicate():
    a = vac(TEXT_A, 101)
    other = vac(TEXT_A.replace("ChainX & Co", "CryptoY Ltd"), 303)
    assert dedup_hash(a) != dedup_hash(other)

    # разные должности в одной компании — НЕ дубль (ключ с должностью)
    other_role = vac(TEXT_A.replace("Senior Solidity <Engineer>", "Junior Rust <Engineer>"), 304)
    assert dedup_hash(a) != dedup_hash(other_role)


def test_no_company_falls_back_to_contact():
    t = "Middle QA | 2-3k$ remote. Писать: @qa_hr"
    v = parse_vacancy_text(t, feed_key="tg", feed_label="")
    assert dedup_hash(v)
    # другой контакт — уже не дубль (другой рекрутер = другая вакансия)
    v2 = parse_vacancy_text(t.replace("@qa_hr", "@other_hr"), feed_key="tg", feed_label="")
    assert dedup_hash(v) != dedup_hash(v2)


def test_empty_title_disables_dedup():
    from types import SimpleNamespace
    assert dedup_hash(SimpleNamespace(title="", company="", contacts=[])) == ""


def test_deliver_marks_second_copy_skipped_duplicate():
    """Конвейер: первая копия доставляется, вторая — skipped_duplicate
    (ни подписчикам, ни в очередь канала), в дайджесте её нет."""
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_dup_")) / "t.db")
    cfg = Config(bot_token="x")
    cfg.tg_channels = ""
    cfg.admin_chat_id = ADMIN

    sent: list[tuple[int, str]] = []

    class FakeSender:
        async def send_vacancy(self, chat_id, v, **kw):
            sent.append((chat_id, v.title))
            return "classic"

        async def send_text(self, chat_id, html, markup=None):
            pass

    mon = Monitor(object(), db, FakeSender(), cfg)
    rec = [{"chat_id": ADMIN, "silent": False, "display": "auto"}]
    db.add_job(101, "a", "https://t.me/x/101", title="Senior Solidity Engineer")
    db.add_job(202, "b", "https://t.me/y/202", title="Senior Solidity Engineer")
    stats = Monitor._new_stats()

    asyncio.run(mon._deliver_vacancy(101, vac(TEXT_A, 101), rec, stats))
    assert stats["sent"] == 1 and db.job_status(101) == "sent"
    asyncio.run(mon._deliver_vacancy(202, vac(TEXT_B, 202), rec, stats))
    assert stats["duplicates"] == 1
    assert db.job_status(202) == "skipped_duplicate"
    assert len(sent) == 1  # вторая копия никуда не ушла
    assert db.get_job(202)["mod_status"] == ""  # и в очередь канала не встала

    # дайджест (/digest) дубль не показывает
    assert [r["job_id"] for r in db.recent_jobs()] == [101]

    # ретрай ДОСТАВЛЕННОЙ (той же) вакансии — не дубль сама себя
    asyncio.run(mon._deliver_vacancy(101, vac(TEXT_A, 101), rec, stats))
    assert stats["duplicates"] == 1
    db.close()


def test_dedup_window_and_delivered_only():
    """Дубли ловятся только среди ДОСТАВЛЕННЫХ и только в окне 7 дней."""
    db = Database(Path(tempfile.mkdtemp(prefix="hirify_dup_")) / "t.db")
    v = vac(TEXT_A, 101)
    h = dedup_hash(v)

    # копия, которая НЕ дошла (skipped_no_contact), дедупом не считается
    db.add_job(301, "c", "u", title=v.title)
    db.set_dedup_hash(301, h)
    db.mark_job(301, "skipped_no_contact")
    assert db.duplicate_of(h, 302) is None

    # доставленная копия блокирует; старше окна — нет («возобновили набор»)
    db.add_job(101, "a", "u", title=v.title)
    db.set_dedup_hash(101, h)
    db.mark_job(101, "sent")
    assert db.duplicate_of(h, 302) == 101
    db.conn.execute("UPDATE jobs SET first_seen='2020-01-01T00:00:00' WHERE job_id=101")
    db.conn.commit()
    assert db.duplicate_of(h, 302) is None
    db.close()


def test_payload_keeps_identity_for_dedup():
    """payload в очереди канала хранит всё для дедупа после рестарта ранов."""
    v = vac(TEXT_A, 101)
    v2 = vacancy_from_payload(vacancy_to_payload(v))
    assert dedup_hash(v2) == dedup_hash(v)
