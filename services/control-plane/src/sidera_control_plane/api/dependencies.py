from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.resource_queries import ResourceQueryService
from sidera_control_plane.application.task_commands import TaskCommandHandler
from sidera_control_plane.application.task_queries import TaskQueryService
from sidera_control_plane.application.worker_commands import WorkerCommandHandler
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.resource_query_store import (
    SqlAlchemyResourceQueryStore,
)
from sidera_control_plane.persistence.task_query_store import SqlAlchemyTaskQueryStore
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_command_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CommandService:
    factory: Callable[[], ControlPlaneUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database
    )
    return CommandService(
        factory,
        (
            TaskCommandHandler(),
            WorkerCommandHandler(lambda: datetime.now(UTC)),
            ExecutionCommandHandler(
                lambda: datetime.now(UTC),
                lease_ttl_seconds=settings.assignment_lease_seconds,
            ),
        ),
    )


def get_task_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> TaskQueryService:
    return TaskQueryService(SqlAlchemyTaskQueryStore(database))


def get_resource_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> ResourceQueryService:
    return ResourceQueryService(SqlAlchemyResourceQueryStore(database))
