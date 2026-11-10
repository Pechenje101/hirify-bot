"""Контейнер общих сервисов, доступный в хендлерах через workflow_data."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .db import Database
from .hirify import HirifyClient
from .monitor import Monitor
from .sender import VacancySender


@dataclass
class Services:
    cfg: Config
    db: Database
    client: HirifyClient
    sender: VacancySender
    monitor: Monitor
