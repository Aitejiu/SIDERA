import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CommandModel,
    EventModel,
    OutboxMessageModel,
    TaskModel,
    WorkspaceModel,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("SIDERA_DATABASE_URL")
    if url is None:
        pytest.fail("SIDERA_DATABASE_URL is required for integration tests")
    return url


@pytest.fixture(scope="session")
def database(database_url: str) -> Iterator[Database]:
    config = Config(str(CONTROL_PLANE_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    instance = Database(database_url)
    yield instance
    instance.dispose()


@pytest.fixture(autouse=True)
def clean_database(database: Database) -> Iterator[None]:
    yield
    with database.session() as session:
        for model in (
            OutboxMessageModel,
            EventModel,
            CommandModel,
            TaskModel,
            WorkspaceModel,
        ):
            session.execute(delete(model))
        session.commit()


@pytest.fixture
def workspace_id(database: Database) -> UUID:
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=WORKSPACE_ID,
                name="Integration Test Workspace",
                status="active",
                version=1,
            )
        )
        session.commit()
    return WORKSPACE_ID
