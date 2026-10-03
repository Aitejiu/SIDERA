import ipaddress
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sidera_agents.api import AgentCatalog, AgentRuntimes
from sidera_agents.ports import ExecutionAuthorizer

from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.adapters.knowledge_http import KnowledgeHttpAdapter
from sidera_control_plane.api.handoff_queries import HandoffPublicQueryService
from sidera_control_plane.application.agent_sources import TaskContextSourceReader
from sidera_control_plane.application.agent_worker_adapter import NativeWorkerServices
from sidera_control_plane.application.artifact_content import ArtifactContentService
from sidera_control_plane.application.assignments import (
    AssignmentCommandHandler,
    TrustedAssignmentIdentity,
)
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
from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
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
from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryService,
)
from sidera_control_plane.application.notification_metrics import (
    NotificationMetricsService,
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
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    KnowledgeCatalogPort,
    ResultInputContentPort,
)
from sidera_control_plane.application.resource_queries import ResourceQueryService
from sidera_control_plane.application.result_acceptance import ResultAcceptanceService
from sidera_control_plane.application.result_input_queries import (
    ResultInputQueryService,
)
from sidera_control_plane.application.result_integration import ResultIntegrationService
from sidera_control_plane.application.result_queries import ResultQueryService
from sidera_control_plane.application.result_rework import ResultReworkService
from sidera_control_plane.application.result_verification import (
    ResultVerificationService,
)
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
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.manifest import (
    DemoConfigurationError,
    load_manifest,
    validate_manifest,
)
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
from sidera_control_plane.persistence.notification_metrics_store import (
    SqlAlchemyNotificationMetricsStore,
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
from sidera_control_plane.persistence.result_input_query_store import (
    SqlAlchemyResultInputQueryStore,
)
from sidera_control_plane.persistence.result_query_store import (
    SqlAlchemyResultQueryStore,
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

_PROJECT_ROOT = Path(__file__).resolve().parents[5]


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_native_worker_services() -> NativeWorkerServices | None:
    """Product composition injects public agents services and a trusted scope."""
    return None


def get_worker_runtime_resolver(
    native_services: NativeWorkerServices | None = None,
) -> WorkerRuntimeResolver:
    """Construct the shared runtime-fact resolver used by command/query edges."""

    return WorkerRuntimeResolver(native_services=native_services)


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


class ResultIntegrationDisabled(ValidationError):
    code = "result_integration_disabled"


def require_result_integration_enabled(settings: Settings) -> None:
    if not settings.result_integration_enabled:
        raise ResultIntegrationDisabled(
            "result integration is disabled by configuration",
            details={"feature": "result_integration"},
        )


class DemoRuntimeBindingForbidden(ValidationError):
    code = "demo_runtime_binding_forbidden"


_DEMO_RUNTIME_COMMAND_TYPES = frozenset(
    {"ConfigureWorkerRuntime", "PinExecutionWorker", "ClearExecutionWorkerPin"}
)
_DEMO_WORKER_ADAPTER_KEYS = {
    "verification": frozenset({"verification"}),
    "validation": frozenset({"worker_primary", "worker_secondary"}),
    "report": frozenset({"worker_primary", "worker_secondary"}),
    "review": frozenset({"worker_primary", "worker_secondary"}),
    "integration": frozenset({"worker_primary", "worker_secondary"}),
    "replacement": frozenset({"worker_primary", "worker_secondary"}),
}
_DEMO_RUNTIME_CONFIG_VERSION = 1


def require_demo_runtime_binding(request: object, settings: Settings) -> None:
    """Reject runtime mutations that escape the explicitly configured demo run."""

    command_type = getattr(request, "command_type", None)
    manifest_path = settings.interview_demo_manifest
    if command_type not in _DEMO_RUNTIME_COMMAND_TYPES or manifest_path is None:
        return

    manifest = _load_validated_demo_manifest(manifest_path)
    workspace_id = getattr(request, "workspace_id", None)
    if workspace_id != manifest.workspace_id:
        raise DemoRuntimeBindingForbidden(
            "demo runtime command workspace does not match the active run",
            details={"workspace_id": str(workspace_id)},
        )

    if command_type == "ConfigureWorkerRuntime":
        _require_demo_runtime_configuration(request, manifest)
        return

    payload = getattr(request, "payload", {})
    if not isinstance(payload, dict):
        raise DemoRuntimeBindingForbidden("demo runtime command payload is invalid")
    if command_type == "PinExecutionWorker":
        worker_id = _demo_uuid(payload.get("worker_id"), "worker_id")
        if worker_id not in manifest.worker_ids.values():
            raise DemoRuntimeBindingForbidden(
                "demo execution pin must target a Worker from this run",
                details={"worker_id": str(worker_id)},
            )


def _load_validated_demo_manifest(path: Path) -> DemoManifest:
    try:
        manifest = load_manifest(path.expanduser())
        validate_manifest(manifest, _PROJECT_ROOT)
        return manifest
    except (DemoConfigurationError, OSError, ValueError) as exc:
        raise DemoRuntimeBindingForbidden(
            "the configured interview demo manifest is invalid",
            details={"reason": type(exc).__name__},
        ) from exc


def _require_demo_runtime_configuration(
    request: object, manifest: DemoManifest
) -> None:
    target_id = getattr(request, "target_id", None)
    if not isinstance(target_id, UUID):
        raise DemoRuntimeBindingForbidden(
            "demo runtime configuration must target a Worker from this run"
        )
    worker_key = next(
        (key for key, value in manifest.worker_ids.items() if value == target_id),
        None,
    )
    if worker_key is None:
        raise DemoRuntimeBindingForbidden(
            "demo runtime configuration must target a Worker from this run",
            details={"worker_id": str(target_id)},
        )
    payload = getattr(request, "payload", {})
    if not isinstance(payload, dict):
        raise DemoRuntimeBindingForbidden(
            "demo runtime configuration payload is invalid"
        )
    allowed_adapter_ids = frozenset(
        manifest.module_ids[key] for key in _DEMO_WORKER_ADAPTER_KEYS[worker_key]
    )
    expected_workspace = manifest.module_ids["workspace"]
    adapter_id = _demo_uuid(
        payload.get("adapter_module_instance_id"), "adapter_module_instance_id"
    )
    workspace_id = _demo_uuid(
        payload.get("workspace_module_instance_id"), "workspace_module_instance_id"
    )
    if adapter_id not in allowed_adapter_ids:
        raise DemoRuntimeBindingForbidden(
            "demo runtime configuration adapter is outside the allowed provider binding",
            details={
                "worker_key": worker_key,
                "adapter_module_instance_id": str(adapter_id),
            },
        )
    if workspace_id != expected_workspace:
        raise DemoRuntimeBindingForbidden(
            "demo runtime configuration workspace provider is outside the active run",
            details={"workspace_module_instance_id": str(workspace_id)},
        )
    for key in (
        "adapter_module_config_version",
        "workspace_module_config_version",
    ):
        if payload.get(key) != _DEMO_RUNTIME_CONFIG_VERSION:
            raise DemoRuntimeBindingForbidden(
                "demo runtime configuration module version is not supported",
                details={"field": key, "expected": _DEMO_RUNTIME_CONFIG_VERSION},
            )


def _demo_uuid(value: object, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise DemoRuntimeBindingForbidden(
            "demo runtime command contains an invalid UUID",
            details={"field": field},
        ) from exc


def get_trusted_assignment_identity() -> TrustedAssignmentIdentity | None:
    """Standalone composition may override this with a verified request principal."""
    return


def get_assignment_authorizer() -> ExecutionAuthorizer | None:
    return None


def get_assignment_agent_catalog() -> AgentCatalog | None:
    return None


def get_assignment_agent_runtimes() -> AgentRuntimes | None:
    return None


def get_agent_context_identity(
    identity: Annotated[
        TrustedAssignmentIdentity | None, Depends(get_trusted_assignment_identity)
    ],
) -> TrustedAssignmentIdentity:
    if identity is None or not identity.principal_id:
        raise HTTPException(status_code=401, detail="Trusted task identity required.")
    return identity


def get_agent_context_knowledge_catalog() -> KnowledgeCatalogPort | None:
    """Product composition injects exact knowledge I/O; no legacy service token."""
    return None


def get_agent_context_result_input_content() -> ResultInputContentPort | None:
    return None


def get_agent_context_source_reader(
    identity: Annotated[TrustedAssignmentIdentity, Depends(get_agent_context_identity)],
    authorizer: Annotated[
        ExecutionAuthorizer | None, Depends(get_assignment_authorizer)
    ],
    database: Annotated[Database, Depends(get_database)],
    catalog: Annotated[
        KnowledgeCatalogPort | None, Depends(get_agent_context_knowledge_catalog)
    ],
    content: Annotated[
        ResultInputContentPort | None, Depends(get_agent_context_result_input_content)
    ],
) -> TaskContextSourceReader:
    if authorizer is None:
        raise HTTPException(status_code=403, detail="Task source access denied.")

    def factory():
        return SqlAlchemyUnitOfWork(database)

    authority_reader = ExecutionAuthorityReader(
        factory,
        principal=lambda: identity.principal_id,
        authorizer=authorizer,
    )
    return TaskContextSourceReader(
        factory,
        authorizer=authority_reader,
        catalog=catalog,
        result_input_content=content,
    )


def get_command_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    assignment_identity: Annotated[
        TrustedAssignmentIdentity | None, Depends(get_trusted_assignment_identity)
    ] = None,
    assignment_authorizer: Annotated[
        ExecutionAuthorizer | None, Depends(get_assignment_authorizer)
    ] = None,
    agent_catalog: Annotated[
        AgentCatalog | None, Depends(get_assignment_agent_catalog)
    ] = None,
    agent_runtimes: Annotated[
        AgentRuntimes | None, Depends(get_assignment_agent_runtimes)
    ] = None,
    native_services: Annotated[
        NativeWorkerServices | None, Depends(get_native_worker_services)
    ] = None,
) -> CommandService:
    assignments = AssignmentCommandHandler(
        catalog=agent_catalog,
        runtimes=agent_runtimes,
        identity=assignment_identity,
        authorizer=assignment_authorizer,
    )
    factory: Callable[[], ControlPlaneUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
    )
    runtime_resolver = get_worker_runtime_resolver(native_services)
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
    planning = PlanningCommandHandler(
        local_actor_type=settings.local_actor_type,
        local_actor_id=settings.local_actor_id,
        clock=lambda: datetime.now(UTC),
        planning_context_ttl_seconds=settings.planning_context_ttl_seconds,
        result_inputs_enabled=settings.result_inputs_enabled,
        candidate_submission_service=KnowledgeCandidateSubmissionService(
            FileCandidateProposalSpool(
                settings.knowledge_candidate_spool_dir,
                quota_bytes=settings.knowledge_candidate_spool_quota_bytes,
            )
        ),
        assignment_handler=assignments,
        identity=assignment_identity,
        authorizer=assignment_authorizer,
    )

    def preflight(uow, envelope):
        assignments.preflight(uow, envelope)
        planning.preflight(uow, envelope)

    return CommandService(
        factory,
        (
            assignments,
            ResultAcceptanceService(
                lambda: datetime.now(UTC),
                local_actor_type=settings.local_actor_type,
                local_actor_id=settings.local_actor_id,
                result_integration_enabled=settings.result_integration_enabled,
            ),
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
                result_integration_enabled=settings.result_integration_enabled,
            ),
            ExecutionInputCommandHandler(lambda: datetime.now(UTC)),
            ResultReworkService(
                lambda: datetime.now(UTC),
                local_actor_type=settings.local_actor_type,
                local_actor_id=settings.local_actor_id,
            ),
            ResultVerificationService(
                factory,
                clock=lambda: datetime.now(UTC),
            ),
            ResultIntegrationService(
                factory,
                clock=lambda: datetime.now(UTC),
            ),
            planning,
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
        preflight=preflight,
    )


def get_command_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> CommandQueryService:
    return CommandQueryService(SqlAlchemyCommandQueryStore(database))


def get_handoff_public_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    native_services: Annotated[
        NativeWorkerServices | None, Depends(get_native_worker_services)
    ] = None,
) -> HandoffPublicQueryService:
    factory: Callable[[], ControlPlaneUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
    )
    runtime_resolver = get_worker_runtime_resolver(native_services)
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


def get_notification_metrics_service(
    database: Annotated[Database, Depends(get_database)],
) -> NotificationMetricsService:
    return NotificationMetricsService(SqlAlchemyNotificationMetricsStore(database))


def get_notification_delivery_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> NotificationDeliveryService:
    factory: Callable[[], ControlPlaneUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
    )
    return NotificationDeliveryService(
        factory,
        clock=lambda: datetime.now(UTC),
        batch_size=settings.notification_delivery_batch_size,
        claim_lease_seconds=settings.notification_claim_lease_seconds,
        max_attempts=settings.notification_delivery_max_attempts,
        max_window_seconds=settings.notification_delivery_max_window_seconds,
        retry_base_seconds=settings.notification_retry_base_seconds,
        retry_backoff_max_seconds=settings.notification_retry_backoff_max_seconds,
    )


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
        result_input_content=get_artifact_content_service(database, settings),
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


def get_result_input_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ResultInputQueryService:
    return ResultInputQueryService(
        SqlAlchemyResultInputQueryStore(database),
        governance_enabled=settings.collaboration_governance_enabled,
    )


def get_result_query_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ResultQueryService:
    storage_inspector = None
    if settings.result_repository_path is not None:
        storage_inspector = GitResultStore(
            {settings.result_repository_ref: settings.result_repository_path},
            git_executable=settings.git_executable,
        )
    return ResultQueryService(
        SqlAlchemyResultQueryStore(database),
        storage_inspector=storage_inspector,
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
