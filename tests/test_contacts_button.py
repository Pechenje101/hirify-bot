"""Кнопка «Показать контакты» (CONTACTS_MODE=button): ядро обработчика.

Карточка уходит из анонимных данных, контакт материализуется ОДНИМ
auth-запросом по нажатию человека и кэшируется в jobs.tg_contacts —
второе нажатие уже ничего не запрашивает.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.hirify import HirifyAutomationError, HirifyRestrictedError  # noqa: E402
from app.monitor import Monitor  # noqa: E402
from app.parsing import Vacancy  # noqa: E402
from app.services import Services  # noqa: E402
from app.handlers import fetch_contacts_for_job  # noqa: E402

import json  # noqa: E402

CONTACTS_OK = {"vacancy_id": 101, "access_type": "free_vacancy",
               "contacts": [{"type": "telegram", "value": "https://t.me/acme_hr"}]}
CONTACTS_EMPTY = {"vacancy_id": 101, "access_type": "free_vacancy", "contacts": []}


class FakeClient:
    def __init__(self, contacts=CONTACTS_OK):
        self.contacts = contacts
        self.contacts_fetches = 0
        self.has_cookie = True
        self.raise_exc: Exception | None = None

    async def fetch_contacts(self, slug: str) -> dict:
        self.contacts_fetches += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        assert "-" in slug and slug.split("-")[0].isdigit()  # ждём <id>-<slug>
        return self.contacts


class FakeSender:
    async def send_vacancy(self, *a, **kw):
        return "classic"

    async def send_text(self, *a, **kw):
        return None


def make_services(contacts=CONTACTS_OK) -> tuple[Services, FakeClient, Monitor, Database]:
    cfg = Config(bot_token="x")
    cfg.contacts_mode = "button"
    cfg.tg_channels = ""
    cfg.auth_views_hour_max = 10**9
    cfg.auth_views_day_max = 10**9
    client = FakeClient(contacts)
    tmpdir = tempfile.mkdtemp(prefix="hirify_btn_")
    db = Database(Path(tmpdir) / "t.db")
    mon = Monitor(client, db, FakeSender(), cfg)
    services = Services(cfg=cfg, db=db, client=client, sender=FakeSender(), monitor=mon)
    # вакансия «доставлена с кнопкой»: контактов в БД нет
    db.add_job(101, "senior-solidity-engineer",
               "https://hirify.me/jobs/101-senior-solidity-engineer",
               title="Senior Solidity Engineer")
    db.mark_job(101, "sent")
    return services, client, mon, db


def test_button_fetch_ok_and_cache():
    """Первое нажатие — один auth-запрос, контакт в ответе и в БД; второе —
    из кэша, сеть не трогаем."""
    services, client, mon, db = make_services()
    out, entries = asyncio.run(fetch_contacts_for_job(101, services))
    assert out == "ok" and client.contacts_fetches == 1
    assert entries[0]["kind"] == "username" and entries[0]["value"] == "acme_hr"
    job = db.get_job(101)
    assert json.loads(job["tg_contacts"])[0]["value"] == "acme_hr"
    out2, entries2 = asyncio.run(fetch_contacts_for_job(101, services))
    assert out2 == "cached" and client.contacts_fetches == 1  # сеть не трогали
    assert entries2 == entries
    db.close()


def test_button_fetch_empty():
    """Сайт контактов не отдал → честный «empty», без записи в БД."""
    services, client, mon, db = make_services(contacts=CONTACTS_EMPTY)
    out, entries = asyncio.run(fetch_contacts_for_job(101, services))
    assert out == "empty" and entries == []
    assert json.loads(db.get_job(101)["tg_contacts"]) == []
    db.close()


def test_button_unknown_job():
    services, _, _, db = make_services()
    out, entries = asyncio.run(fetch_contacts_for_job(999999, services))
    assert out == "noid" and entries == []
    db.close()


def test_button_paused_by_flag():
    """Пауза флага: кнопка отвечает «позже» и НЕ делает запрос аккаунта."""
    services, client, mon, db = make_services()
    mon._automation_until = time.time() + 3600
    mon._automation_last_flag = time.time()
    out, entries = asyncio.run(fetch_contacts_for_job(101, services))
    assert out.startswith("pause:") and entries == []
    assert client.contacts_fetches == 0
    db.close()


def test_button_quota_blocks():
    """Квоты auth-просмотров касаются и кнопки: часовой лимит закрыт —
    запроса нет."""
    services, client, mon, db = make_services()
    mon.cfg.auth_views_hour_max = 1
    mon.auth_view_spent()  # первый (и единственный) просмотр уже потрачен
    out, _ = asyncio.run(fetch_contacts_for_job(101, services))
    assert out == "quota" and client.contacts_fetches == 0
    db.close()


def test_button_no_cookie():
    services, client, mon, db = make_services()
    client.has_cookie = False
    out, _ = asyncio.run(fetch_contacts_for_job(101, services))
    assert out == "noauth" and client.contacts_fetches == 0
    db.close()


def test_button_flag_from_fetch():
    """Запрос по кнопке словил флаг → пауза поставлена (эскалация живёт
    в БД), исход «flag:N» — человек увидит «попробуйте позже»."""
    services, client, mon, db = make_services()
    client.raise_exc = HirifyAutomationError("automation_detected")
    out, _ = asyncio.run(fetch_contacts_for_job(101, services))
    assert out.startswith("flag:")
    assert mon.automation_left_sec > 0          # пауза поставлена
    assert int(db.get_setting("automation_flags", "0")) >= 1
    db.close()


def test_button_restricted():
    services, client, mon, db = make_services()
    client.raise_exc = HirifyRestrictedError("403 restricted")
    out, _ = asyncio.run(fetch_contacts_for_job(101, services))
    assert out == "restricted"
    db.close()
