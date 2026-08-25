from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from sidera_control_plane.application.artifact_content import ArtifactContentService
from sidera_control_plane.application.attention_commands import (
    AcknowledgeAttentionItemHandler,
)
from sidera_control_plane.application.command_queries import CommandQueryService
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import (
    ExecutionCommandHandler,
    ExecutionInputCommandHandler,
)
from sidera_control_plane.application.module_queries import ModuleQueryService
from sidera_control_plane.application.plan_commands import PublishPlanSnapshotHandler
from sidera_control_plane.application.planning_commands import PlanningCommandHandler
from sidera_control_plane.application.planning_metrics import PlanningMetricsService
from sidera_control_plane.application.planning_queries import PlanningQueryService
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.resource_queries import ResourceQueryService
from sidera_control_plane.application.runtime_commands import RuntimeCommandHandler
from sidera_control_plane.application.runtime_metrics import RuntimeMetricsService
from sidera_control_plane.application.subtask_workspace_queries import (
    SubtaskWorkspaceQueryService,
)
from sidera_control_plane.application.task_commands import TaskCommandHandler
from sidera_control_plane.application.task_console_queries import (
    TaskConsoleQueryService,
)
from sidera_control_plane.application.task_first_queries import TaskFirstQueryService
from sidera_control_plane.application.task_queries import TaskQueryService
from sidera_control_plane.application.work_context_commands import (
    WorkContextCommandHandler,
)
from sidera_control_plane.application.work_context_queries import (
    WorkContextQueryService,
)
from sidera_control_plane.application.worker_commands import WorkerCommandHandler
from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
from sidera_control_plane.application.worker_trace_queries import (
    WorkerTraceQueryService,
)
from sidera_control_plane.application.worker_workspace_queries import (
    WorkerWorkspaceQueryService,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.command_query_store import (
    SqlAlchemyCommandQueryStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.planning_query_store import (
    SqlAlchemyPlanningMetricsStore,
    SqlAlchemyPlanningQueryStore,
)
from sidera_control_plane.persistence.resource_query_store import (
    SqlAlchemyModuleQueryStore,
    SqlAlchemyResourceQueryStore,
)
from sidera_control_plane.persistence.runtime_metrics_store import (
    SqlAlchemyRuntimeMetricsStore,
)
from sidera_control_plane.persistence.subtask_workspace_query_store import (
    SqlAlchemySubtaskWorkspaceQueryStore,
)
from sidera_control_plane.persistence.task_console_query_store import (
    SqlAlchemyTaskConsoleQueryStore,
)
from sidera_control_plane.persistence.task_first_query_store import (
    SqlAlchemyTaskFirstQueryStore,
)
from sidera_control_plane.persistence.task_query_store import SqlAlchemyTaskQueryStore
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from sidera_control_plane.persistence.worker_workspace_query_store import (
    SqlAlchemyWorkerWorkspaceQueryStore,
)


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
            AcknowledgeAttentionItemHandler(lambda: datetime.now(UTC)),
            PublishPlanSnapshotHandler(),
            WorkerCommandHandler(lambda: datetime.now(UTC)),
            RuntimeCommandHandler(
                lambda: datetime.now(UTC),
                resolver=WorkerRuntimeResolver(),
            ),
            ExecutionCommandHandler(
                lambda: datetime.now(UTC),
                lease_ttl_seconds=settings.assignment_lease_seconds,
            ),
            ExecutionInputCommandHandler(lambda: datetime.now(UTC)),
            PlanningCommandHandler(
                local_actor_type=settings.local_actor_type,
                local_actor_id=settings.local_actor_id,
                clock=lambda: datetime.now(UTC),
            ),
            WorkContextCommandHandler(
                local_actor_type=settings.local_actor_type,
                local_actor_id=settings.local_actor_id,
                clock=lambda: datetime.now(UTC),
            ),
        ),
    )


def get_command_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> CommandQueryService:
    return CommandQueryService(SqlAlchemyCommandQueryStore(database))


def get_task_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> TaskQueryService:
    return TaskQueryService(SqlAlchemyTaskQueryStore(database))


def get_planning_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> PlanningQueryService:
    return PlanningQueryService(SqlAlchemyPlanningQueryStore(database))


def get_work_context_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> WorkContextQueryService:
    from sidera_control_plane.persistence.work_context_query_store import (
        SqlAlchemyWorkContextQueryStore,
    )

    return WorkContextQueryService(SqlAlchemyWorkContextQueryStore(database))


def get_planning_metrics_service(
    database: Annotated[Database, Depends(get_database)],
) -> PlanningMetricsService:
    return PlanningMetricsService(SqlAlchemyPlanningMetricsStore(database))


def get_runtime_metrics_service(
    database: Annotated[Database, Depends(get_database)],
) -> RuntimeMetricsService:
    return RuntimeMetricsService(SqlAlchemyRuntimeMetricsStore(database))


def get_resource_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> ResourceQueryService:
    return ResourceQueryService(SqlAlchemyResourceQueryStore(database))


def get_module_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> ModuleQueryService:
    return ModuleQueryService(SqlAlchemyModuleQueryStore(database))


def get_task_first_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TaskFirstQueryService:
    return TaskFirstQueryService(
        SqlAlchemyTaskFirstQueryStore(database),
        main_agent_entry_uri=settings.main_agent_entry_uri,
        actor_type=settings.local_actor_type,
        actor_id=settings.local_actor_id,
    )


def get_task_console_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TaskConsoleQueryService:
    return TaskConsoleQueryService(
        SqlAlchemyTaskConsoleQueryStore(database),
        actor_type=settings.local_actor_type,
        actor_id=settings.local_actor_id,
    )


def get_subtask_workspace_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> SubtaskWorkspaceQueryService:
    return SubtaskWorkspaceQueryService(
        SqlAlchemySubtaskWorkspaceQueryStore(database)
    )


def get_worker_trace_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> WorkerTraceQueryService:
    return WorkerTraceQueryService(SqlAlchemyTaskFirstQueryStore(database))


def get_worker_workspace_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> WorkerWorkspaceQueryService:
    return WorkerWorkspaceQueryService(
        SqlAlchemyWorkerWorkspaceQueryStore(database)
    )


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
