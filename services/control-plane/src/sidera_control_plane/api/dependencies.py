from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from sidera_control_plane.application.artifact_content import ArtifactContentService
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import (
    ExecutionCommandHandler,
    ExecutionInputCommandHandler,
)
from sidera_control_plane.application.plan_commands import PublishPlanSnapshotHandler
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.resource_queries import ResourceQueryService
from sidera_control_plane.application.task_commands import TaskCommandHandler
from sidera_control_plane.application.task_first_queries import TaskFirstQueryService
from sidera_control_plane.application.task_queries import TaskQueryService
from sidera_control_plane.application.worker_commands import WorkerCommandHandler
from sidera_control_plane.application.worker_trace_queries import (
    WorkerTraceQueryService,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.resource_query_store import (
    SqlAlchemyResourceQueryStore,
)
from sidera_control_plane.persistence.task_first_query_store import (
    SqlAlchemyTaskFirstQueryStore,
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
        database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
    )
    return CommandService(
        factory,
        (
            TaskCommandHandler(),
            PublishPlanSnapshotHandler(),
            WorkerCommandHandler(lambda: datetime.now(UTC)),
            ExecutionCommandHandler(
                lambda: datetime.now(UTC),
                lease_ttl_seconds=settings.assignment_lease_seconds,
            ),
            ExecutionInputCommandHandler(lambda: datetime.now(UTC)),
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


def get_task_first_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TaskFirstQueryService:
    return TaskFirstQueryService(
        SqlAlchemyTaskFirstQueryStore(database),
        main_agent_entry_uri=settings.main_agent_entry_uri,
    )


def get_worker_trace_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> WorkerTraceQueryService:
    return WorkerTraceQueryService(SqlAlchemyTaskFirstQueryStore(database))


def get_artifact_content_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ArtifactContentService:
    return ArtifactContentService(
        database,
        LocalArtifactContentStore(
            settings.artifact_root,
            max_bytes=settings.artifact_max_bytes,
        ),
        token_secret=settings.artifact_download_token_secret,
    )
