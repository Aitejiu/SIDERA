from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from sidera_knowledge.persistence.database import Database

KNOWLEDGE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def database_url() -> str:
    value = os.environ.get("SIDERA_KNOWLEDGE_DATABASE_URL")
    if value is None:
        pytest.fail("SIDERA_KNOWLEDGE_DATABASE_URL is required for integration tests")
    return value


@pytest.fixture(scope="session")
def database(database_url: str) -> Iterator[Database]:
    config = Config(str(KNOWLEDGE_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    database = Database(database_url)
    yield database
    database.dispose()
