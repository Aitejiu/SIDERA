import ipaddress
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import Depends, Request

from sidera_control_plane.adapters.knowledge_http import KnowledgeHttpAdapter
from sidera_control_plane.api.handoff_queries import HandoffPublicQueryService
from sidera_control_plane.application.artifact_content import ArtifactContentService
from sidera_control_plane.application.attention_commands import (
    AcknowledgeAttentionItemHandler,
)
from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandHandler,
)
from sidera_control_plane.application.collaboration_governance_release import (
    CollaborationGovernanceReleaseApplier,
)
from sidera_control_plane.application.collaboration_projection import (
    CollaborationProjectionAssembler,
)
from sidera_control_plane.application.collaboration_queries import (
    CollaborationQueryService,
)
from sidera_control_plane.application.command_queries import CommandQueryService
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.context_package_queries import (
    ContextPackageCommandHandler,
    ContextPackageQueryService,
)
from sidera_control_plane.application.execution_commands import (
    ExecutionCommandHandler,
    ExecutionInputCommandHandler,
)
from sidera_control_plane.application.handoff_candidates import HandoffCandidateService
from sidera_control_plane.application.handoff_commands import HandoffCommandHandler
from sidera_control_plane.application.handoff_queries import (
    HandoffQueryService as ActiveHandoffQueryService,
)
from sidera_control_plane.application.handoff_recovery import HandoffRecoveryService
from sidera_control_plane.application.knowledge_candidate_submission import (
    FileCandidateProposalSpool,
    KnowledgeCandidateSubmissionService,
)
from sidera_control_plane.application.module_queries import ModuleQueryService
from sidera_control_plane.application.notification_commands import (
    NotificationCommandHandler,
)
from sidera_control_plane.application.notification_queries import (
    NotificationQueryService,
)
from sidera_control_plane.application.plan_commands import PublishPlanSnapshotHandler
from sidera_control_plane.application.plan_revision_queries import (
    PlanRevisionQueryService,
)
from sidera_control_plane.application.plan_transition_commands import (
    PlanTransitionCommandHandler,
)
from sidera_control_plane.application.plan_transition_queries import (
    PlanTransitionQueryService,
)
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
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.collaboration_query_store import (
    SqlAlchemyCollaborationQueryStore,
)
from sidera_control_plane.persistence.command_query_store import (
    SqlAlchemyCommandQueryStore,
)
from sidera_control_plane.persistence.context_package_query_store import (
    SqlAlchemyContextPackageQueryStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.handoff_query_store import (
    SqlAlchemyHandoffQueryStore,
)
from sidera_control_plane.persistence.notification_query_store import (
    SqlAlchemyNotificationQueryStore,
)
from sidera_control_plane.persistence.plan_revision_query_store import (
    SqlAlchemyPlanRevisionQueryStore,
)
from sidera_control_plane.persistence.plan_transition_query_store import (
    SqlAlchemyPlanTransitionQueryStore,
)
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


class AdapterWorkflowRequiresLoopback(ValidationError):
    code = "adapter_workflow_requires_loopback"


def require_loopback_adapter_workflow(settings: Settings) -> None:
    bind_host = settings.api_bind_host.strip().lower().strip("[]")
    if bind_host == "localhost":
        return
    try:
        is_loopback = ipaddress.ip_address(bind_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise AdapterWorkflowRequiresLoopback(
            "adapter workflow endpoints require a loopback api_bind_host",
            details={"api_bind_host": settings.api_bind_host},
        )


def get_command_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CommandService:
    factory: Callable[[], ControlPlaneUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
    )
    runtime_resolver = WorkerRuntimeResolver()
    handoff_candidates = HandoffCandidateService(
        factory,
        runtime_resolver=runtime_resolver,
    )
    handoff_recovery = HandoffRecoveryService(
        factory,
        runtime_resolver=runtime_resolver,
    )
    governance_approver = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
            session=cast(SqlAlchemyUnitOfWork, uow).session
        ),
        projection_assembler=CollaborationProjectionAssembler(
            max_projection_bytes=settings.collaboration_max_projection_bytes
        ),
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
                resolver=runtime_resolver,
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
                planning_context_ttl_seconds=settings.planning_context_ttl_seconds,
                candidate_submission_service=KnowledgeCandidateSubmissionService(
                    FileCandidateProposalSpool(
                        settings.knowledge_candidate_spool_dir,
                        quota_bytes=settings.knowledge_candidate_spool_quota_bytes,
                    )
                ),
            ),
            NotificationCommandHandler(
                local_actor_type=settings.local_actor_type,
                local_actor_id=settings.local_actor_id,
                clock=lambda: datetime.now(UTC),
            ),
            WorkContextCommandHandler(
                local_actor_type=settings.local_actor_type,
                local_actor_id=settings.local_actor_id,
                clock=lambda: datetime.now(UTC),
            ),
            ContextPackageCommandHandler(
                local_actor_type=settings.local_actor_type,
                local_actor_id=settings.local_actor_id,
                clock=lambda: datetime.now(UTC),
            ),
            PlanTransitionCommandHandler(lambda: datetime.now(UTC)),
            HandoffCommandHandler(
                lambda: datetime.now(UTC),
                candidate_service=handoff_candidates,
                runtime_resolver=runtime_resolver,
                recovery_service=handoff_recovery,
            ),
            CollaborationGovernanceCommandHandler(
                lambda: datetime.now(UTC),
                local_actor_type=settings.local_actor_type,
                local_actor_id=settings.local_actor_id,
                enabled=settings.collaboration_governance_enabled,
                approver=governance_approver,
            ),
        ),
    )


def get_command_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> CommandQueryService:
    return CommandQueryService(SqlAlchemyCommandQueryStore(database))


def get_handoff_public_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HandoffPublicQueryService:
    factory: Callable[[], ControlPlaneUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
    )
    runtime_resolver = WorkerRuntimeResolver()
    candidate_service = HandoffCandidateService(
        factory,
        runtime_resolver=runtime_resolver,
    )
    recovery_service = HandoffRecoveryService(
        factory,
        runtime_resolver=runtime_resolver,
    )
    return HandoffPublicQueryService(
        factory,
        candidate_service=candidate_service,
        active_handoff_service=ActiveHandoffQueryService(
            SqlAlchemyHandoffQueryStore(database)
        ),
        recovery_service=recovery_service,
        runtime_resolver=runtime_resolver,
    )


def get_task_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> TaskQueryService:
    return TaskQueryService(SqlAlchemyTaskQueryStore(database))


def get_planning_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> PlanningQueryService:
    return PlanningQueryService(SqlAlchemyPlanningQueryStore(database))


def get_notification_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> NotificationQueryService:
    return NotificationQueryService(SqlAlchemyNotificationQueryStore(database))


def get_plan_revision_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> PlanRevisionQueryService:
    return PlanRevisionQueryService(SqlAlchemyPlanRevisionQueryStore(database))


def get_plan_transition_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> PlanTransitionQueryService:
    return PlanTransitionQueryService(SqlAlchemyPlanTransitionQueryStore(database))


def get_work_context_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> WorkContextQueryService:
    from sidera_control_plane.persistence.work_context_query_store import (
        SqlAlchemyWorkContextQueryStore,
    )

    return WorkContextQueryService(SqlAlchemyWorkContextQueryStore(database))


def get_context_package_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ContextPackageQueryService:
    def catalog_factory() -> KnowledgeHttpAdapter:
        return KnowledgeHttpAdapter(
            base_url=settings.knowledge_base_url,
            control_plane_token=settings.knowledge_control_plane_token,
            metadata_timeout_seconds=settings.knowledge_metadata_timeout_seconds,
            materialization_timeout_seconds=settings.knowledge_materialization_timeout_seconds,
        )

    return ContextPackageQueryService(
        SqlAlchemyContextPackageQueryStore(database),
        catalog_factory=catalog_factory,
        actor_type=settings.local_actor_type,
        actor_id=settings.local_actor_id,
        clock=lambda: datetime.now(UTC),
    )


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


def get_collaboration_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CollaborationQueryService:
    return CollaborationQueryService(
        SqlAlchemyCollaborationQueryStore(database),
        governance_enabled=settings.collaboration_governance_enabled,
    )


def get_task_console_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TaskConsoleQueryService:
    return TaskConsoleQueryService(
        SqlAlchemyTaskConsoleQueryStore(database),
        actor_type=settings.local_actor_type,
        actor_id=settings.local_actor_id,
        governance_enabled=settings.collaboration_governance_enabled,
    )


def get_subtask_workspace_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SubtaskWorkspaceQueryService:
    return SubtaskWorkspaceQueryService(
        SqlAlchemySubtaskWorkspaceQueryStore(database),
        governance_enabled=settings.collaboration_governance_enabled,
    )


def get_worker_trace_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> WorkerTraceQueryService:
    return WorkerTraceQueryService(SqlAlchemyTaskFirstQueryStore(database))


def get_worker_workspace_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WorkerWorkspaceQueryService:
    return WorkerWorkspaceQueryService(
        SqlAlchemyWorkerWorkspaceQueryStore(database),
        governance_enabled=settings.collaboration_governance_enabled,
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
