import os
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, update

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    EventModel,
    ExecutionModel,
    ExecutionSessionModel,
    OutboxMessageModel,
    TaskModel,
    WorkerModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

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
        session.execute(update(ExecutionModel).values(active_lease_id=None))
        for model in (
            ExecutionSessionModel,
            AssignmentLeaseModel,
            ExecutionModel,
            WorkerModel,
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


@pytest.fixture
def uow_factory(database: Database) -> Callable[[], SqlAlchemyUnitOfWork]:
    return lambda: SqlAlchemyUnitOfWork(database)


@pytest.fixture
def row_counts(database: Database) -> Callable[[], dict[str, int]]:
    def count_rows() -> dict[str, int]:
        with database.session() as session:
            return {
                "tasks": session.scalar(
                    select(func.count()).select_from(TaskModel)
                )
                or 0,
                "commands": session.scalar(
                    select(func.count()).select_from(CommandModel)
                )
                or 0,
                "events": session.scalar(
                    select(func.count()).select_from(EventModel)
                )
                or 0,
                "outbox_messages": session.scalar(
                    select(func.count()).select_from(OutboxMessageModel)
                )
                or 0,
            }

    return count_rows
