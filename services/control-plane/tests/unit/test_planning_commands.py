from collections.abc import Mapping
from copy import copy, deepcopy
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sidera_agents.contracts import (
    ContextItem,
    ExecutionWorkspace,
    ProfileRef,
    RunPolicy,
    RunStart,
    RuntimeRef,
    StartReceipt,
    StartStatus,
)
from sidera_agents.errors import AgentError

from sidera_control_plane.api.schemas import (
    ActorRequest,
    CommandRequest,
    RegisterPlanningProfilePayload,
)
from sidera_control_plane.application.agent_sources import PlanningContextSourceReader
from sidera_control_plane.application.agent_worker_adapter import request_fingerprint
from sidera_control_plane.application.assignments import TrustedAssignmentIdentity
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.context_requests import (
    build_planning_context_assembly_request,
)
from sidera_control_plane.application.errors import IdempotencyConflict, NotFound
from sidera_control_plane.application.notification_commands import (
    NotificationRouteConflict,
)
from sidera_control_plane.application.planning_commands import (
    PlanningCommandHandler,
    PlanningCommandTarget,
)
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.agent_bindings import AssignmentDenied
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.errors import (
    OperatorRequired,
    PlanGenerationConflict,
    PlanningConflict,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.modules import (
    COLLABORATION_RUNTIME_MODULE_TYPE,
    CancellationCapability,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.notifications import (
    NotificationBindingKind,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationSourceBinding,
)
from sidera_control_plane.domain.plan_revisions import (
    RootTaskPlanHead,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningClarification,
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
    PlanningRunLease,
    PlanningRunLeaseStatus,
    PlanningRunStatus,
    PlanningUsage,
    TaskGoalSubmission,
)
from sidera_control_plane.domain.planning_authority import (
    NativePlanningCleanupPending,
    PlanningAgentLink,
    PlanningAuthority,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.work_context import (
    AcceptedDecision,
    ClarificationDecision,
    ContextTextItem,
    KnowledgeRevisionRef,
    UnresolvedQuestion,
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextStatus,
    WorkReference,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("70000000-0000-0000-0000-000000000001")
PROPOSAL_HASH = "a" * 64


def _failed_feedback_command_values():
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(WORKSPACE_ID),
        "command_type": "ReviseFailedPlanningRequest",
        "target_type": "planning_request",
        "target_id": str(uuid4()),
        "expected_version": 7,
        "actor": {"type": "user", "id": "operator"},
        "idempotency_key": uuid4().hex,
        "correlation_id": str(uuid4()),
        "payload": {
            "failed_planning_run_id": str(uuid4()),
            "expected_failed_planning_run_version": 4,
            "expected_work_context_version": 3,
            "work_context_revision_id": str(uuid4()),
            "work_context_content_hash": "a" * 64,
            "feedback": "Keep the four-worker contract.",
        },
    }


def test_failed_planning_feedback_schema_accepts_exact_six_field_payload():
    values = _failed_feedback_command_values()
    parsed = CommandRequest.model_validate(values)
    assert parsed.target_type == "planning_request"
    assert parsed.expected_version == 7
    assert set(parsed.payload) == set(values["payload"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("failed_planning_run_id", "invalid"),
        ("work_context_revision_id", "invalid"),
        ("work_context_content_hash", "A" * 64),
        ("work_context_content_hash", "a" * 63),
        ("work_context_content_hash", "g" * 64),
        ("feedback", " \n\t "),
        ("feedback", None),
        ("closed", True),
    ]
    + [
        (field, value)
        for field in (
            "expected_failed_planning_run_version",
            "expected_work_context_version",
        )
        for value in (True, False, "3", 3.0, 0, -1, None)
    ],
)
def test_failed_planning_feedback_schema_rejects_invalid_payload(field, value):
    values = _failed_feedback_command_values()
    values["payload"][field] = value
    with pytest.raises(ValueError):
        CommandRequest.model_validate(values)


@pytest.mark.parametrize(
    "field",
    [
        "failed_planning_run_id",
        "expected_failed_planning_run_version",
        "expected_work_context_version",
        "work_context_revision_id",
        "work_context_content_hash",
        "feedback",
    ],
)
def test_failed_planning_feedback_schema_requires_every_payload_field(field):
    values = _failed_feedback_command_values()
    del values["payload"][field]
    with pytest.raises(ValueError):
        CommandRequest.model_validate(values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_type": "planning_run"},
        {"target_id": None},
        {"target_id": "invalid"},
        {"expected_version": None},
        {"unexpected": True},
    ]
    + [{"expected_version": value} for value in (True, False, "7", 7.0, 0, -1)],
)
def test_failed_planning_feedback_schema_requires_exact_target_version(overrides):
    values = _failed_feedback_command_values()
    values.update(overrides)
    with pytest.raises(ValueError):
        CommandRequest.model_validate(values)


@pytest.mark.parametrize(
    "field",
    [
        "command_id",
        "workspace_id",
        "command_type",
        "target_type",
        "target_id",
        "expected_version",
        "actor",
        "idempotency_key",
        "correlation_id",
        "payload",
    ],
)
def test_failed_planning_feedback_schema_requires_full_envelope(field):
    values = _failed_feedback_command_values()
    del values[field]
    with pytest.raises(ValueError):
        CommandRequest.model_validate(values)


def planner_capabilities(
    proposal_schema_versions: tuple[int, ...] = (2,),
) -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=proposal_schema_versions,
    )


def module_facts(
    *, proposal_schema_versions: tuple[int, ...] = (2,)
) -> tuple[ModuleDefinition, ModuleInstance]:
    definition_id = uuid4()
    instance_id = uuid4()
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=WORKSPACE_ID,
        module_type="planner_adapter",
        provider="fake",
        interface_version="sidera.planner.v1",
        config_schema_version=1,
        capabilities=planner_capabilities(proposal_schema_versions),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=instance_id,
        workspace_id=WORKSPACE_ID,
        definition_id=definition_id,
        instance_name="test-planner",
        config_version=1,
        config_namespace="planning",
        config={"planning.mode": "test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    return definition, instance


def profile(instance_id: UUID, *, proposal_schema_version: int = 2) -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_key="default",
        version=1,
        display_name="Default planner",
        planner_module_instance_id=instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={"approval": {"required": True}},
        proposal_schema_version=proposal_schema_version,
        allowed_capabilities=("code",),
        adapter_preferences=("opencode",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=10_000,
            max_cost_microunits=1_000_000,
            max_runtime_seconds=600,
            max_revisions=3,
            max_nodes=20,
            max_concurrency=2,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(5,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=(),
    )


class ModuleRepository:
    def __init__(
        self,
        definition: ModuleDefinition,
        instance: ModuleInstance,
        *,
        extra_definitions: tuple[ModuleDefinition, ...] = (),
        extra_instances: tuple[ModuleInstance, ...] = (),
    ) -> None:
        self.definition = definition
        self.instance = instance
        self.definitions = {definition.id: definition}
        self.definitions.update({item.id: item for item in extra_definitions})
        self.instances = {instance.id: instance}
        self.instances.update({item.id: item for item in extra_instances})
        self.added: list[object] = []

    def get_definition(self, module_id: UUID, workspace_id: UUID):
        return self.definitions.get(module_id)

    def get_instance(self, module_id: UUID, workspace_id: UUID):
        return self.instances.get(module_id)

    def add_definition(self, definition: ModuleDefinition) -> None:
        self.added.append(definition)

    def add_instance(self, instance: ModuleInstance) -> None:
        self.added.append(instance)


class ProfileRepository:
    def __init__(self, stored: PlanningProfile | None = None) -> None:
        self.stored = stored
        self.added: list[PlanningProfile] = []

    def add(self, item: PlanningProfile) -> None:
        self.added.append(item)
        self.stored = item

    def get(self, profile_id: UUID, workspace_id: UUID, *, version: int | None = None):
        item = self.stored
        if item is None or item.id != profile_id or item.workspace_id != workspace_id:
            return None
        return item if version is None or item.version == version else None

    def list_selectable(self, workspace_id: UUID):
        return (self.stored,) if self.stored is not None else ()


class RequestRepository:
    def __init__(self, request: PlanningRequest | None = None) -> None:
        self.request = request
        self.active_revision: PlanningRequest | None = None
        self.added: list[PlanningRequest] = []
        self.saved: list[tuple[PlanningRequest, int]] = []

    def add(self, request: PlanningRequest) -> None:
        self.request = request
        self.added.append(request)

    def get(self, request_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        if self.request is None:
            return None
        if self.request.id == request_id and self.request.workspace_id == workspace_id:
            return self.request
        return None

    def save(self, request: PlanningRequest, previous_version: int) -> None:
        self.request = request
        self.saved.append((request, previous_version))

    def get_active_revision_for_root(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRequest | None:
        del for_update
        revision = self.active_revision
        if (
            revision is not None
            and revision.workspace_id == workspace_id
            and revision.root_task_id == root_task_id
        ):
            return revision
        return None


class TaskRepository:
    def __init__(self, task: Task) -> None:
        self.task = task

    def get(self, task_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        del for_update
        if self.task.id == task_id and self.task.workspace_id == workspace_id:
            return self.task
        return None


class PlanRepository:
    def __init__(self, snapshot: TaskPlanSnapshot, head: RootTaskPlanHead) -> None:
        self.snapshot = snapshot
        self.head = head

    def get_head(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ):
        del for_update
        if (
            self.head.root_task_id == root_task_id
            and self.head.workspace_id == workspace_id
        ):
            return self.head
        return None

    def get(self, snapshot_id: UUID, workspace_id: UUID):
        if (
            self.snapshot.id == snapshot_id
            and self.snapshot.workspace_id == workspace_id
        ):
            return self.snapshot
        return None


class RunRepository:
    def __init__(self, request: PlanningRequest | None = None) -> None:
        self.request = request
        self.added: list[object] = []
        self.saved: list[tuple[object, int]] = []

    def add(self, run: object) -> None:
        self.added.append(run)

    def get(self, run_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        if self.request is None:
            return None
        return next(
            (
                run
                for run in self.request.runs
                if run.id == run_id and run.workspace_id == workspace_id
            ),
            None,
        )

    def save(self, run: object, previous_version: int) -> None:
        self.saved.append((run, previous_version))


class LeaseRepository:
    def __init__(self) -> None:
        self.saved: list[tuple[object, int]] = []

    def get(self, lease_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        return None

    def save(self, lease: object, previous_version: int) -> None:
        self.saved.append((lease, previous_version))


class RecordRepository:
    def __init__(self, current_proposal: object | None = None) -> None:
        self.current_proposal = current_proposal
        self.briefs: list[PlanningBriefRevision] = []
        self.clarifications: list[PlanningClarification] = []
        self.saved_clarifications: list[tuple[PlanningClarification, int]] = []

    def add_brief(self, brief: PlanningBriefRevision) -> None:
        self.briefs.append(brief)

    def get_brief(self, request_id: UUID, workspace_id: UUID, revision: int):
        return next((item for item in self.briefs if item.revision == revision), None)

    def list_briefs(self, request_id: UUID, workspace_id: UUID):
        return tuple(self.briefs)

    def add_proposal(self, proposal: object, workspace_id: UUID) -> None:
        raise AssertionError("planning command tests do not create proposals")

    def get_proposal(self, proposal_id: UUID, workspace_id: UUID):
        return self.current_proposal

    def list_proposals(self, request_id: UUID, workspace_id: UUID):
        return (self.current_proposal,) if self.current_proposal is not None else ()

    def get_current_proposal(self, request_id: UUID, workspace_id: UUID):
        return self.current_proposal

    def add_clarification(self, clarification: PlanningClarification) -> None:
        self.clarifications.append(clarification)

    def list_pending_clarifications(self, request_id: UUID, workspace_id: UUID):
        return tuple(item for item in self.clarifications if item.answer is None)

    def save_clarification(
        self, clarification: PlanningClarification, previous_version: int
    ) -> None:
        self.saved_clarifications.append((clarification, previous_version))


class WorkContextRepository:
    def __init__(self, request: PlanningRequest | None = None) -> None:
        self.context: WorkContext | None = None
        self.revisions: dict[UUID, WorkContextRevision] = {}
        self.added: list[tuple[WorkContext, WorkContextRevision]] = []
        self.appended: list[tuple[WorkContextRevision, int]] = []
        self.lookups: list[tuple[str, UUID, UUID, bool]] = []
        if request is not None:
            revision = WorkContextRevisionFactory().create(
                work_context_id=uuid4(),
                workspace_id=request.workspace_id,
                brief=request.briefs[0],
                revision_number=1,
                reason=WorkContextRevisionReason.REQUEST_CREATED,
                actor=request.actor,
                created_at=request.created_at,
            )
            self.context = WorkContext.create(
                work_context_id=revision.work_context_id,
                workspace_id=request.workspace_id,
                planning_request_id=request.id,
                first_revision=revision,
                created_at=request.created_at,
            )
            self.revisions[revision.revision_id] = revision

    def add(self, context: WorkContext, first_revision: WorkContextRevision) -> None:
        self.context = context
        self.revisions[first_revision.revision_id] = first_revision
        self.added.append((context, first_revision))

    def get_by_planning_request(
        self, planning_request_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> WorkContext | None:
        self.lookups.append(("request", planning_request_id, workspace_id, for_update))
        if self.context is None:
            return None
        if (
            self.context.planning_request_id == planning_request_id
            and self.context.workspace_id == workspace_id
        ):
            return self.context
        return None

    def get_by_root_task(
        self, root_task_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> WorkContext | None:
        self.lookups.append(("root", root_task_id, workspace_id, for_update))
        if (
            self.context is not None
            and self.context.root_task_id == root_task_id
            and self.context.workspace_id == workspace_id
        ):
            return self.context
        return None

    def get_revision(
        self, revision_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> WorkContextRevision | None:
        revision = self.revisions.get(revision_id)
        if revision is None or revision.workspace_id != workspace_id:
            return None
        return revision

    def append_revision(
        self,
        context: WorkContext,
        revision: WorkContextRevision,
        *,
        expected_version: int,
    ) -> None:
        context.current_revision_id = revision.revision_id
        context.current_revision_number = revision.revision_number
        self.revisions[revision.revision_id] = revision
        self.appended.append((revision, expected_version))


class ContextAssemblyRepository:
    def __init__(self) -> None:
        self.added: list[ContextAssemblyRequest] = []
        self.stored: ContextAssemblyRequest | None = None
        self.expected_consumer: tuple[ContextPurpose, UUID, UUID] | None = None
        self.reads: list[tuple[ContextPurpose, UUID, UUID, bool]] = []

    def add(self, request: ContextAssemblyRequest) -> ContextAssemblyRequest:
        self.added.append(request)
        return request

    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ContextAssemblyRequest | None:
        assert (purpose, consumer_id, workspace_id) == self.expected_consumer
        assert for_update is True
        self.reads.append((purpose, consumer_id, workspace_id, for_update))
        return self.stored


class EventRepository:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class ProjectionPublisher:
    def __init__(self) -> None:
        self.changes: list[dict[str, object]] = []

    def changed(self, **kwargs: object):
        self.changes.append(kwargs)
        return SimpleNamespace()


class NotificationRepository:
    def __init__(self, routes: tuple[NotificationRoute, ...] = ()) -> None:
        self.routes = list(routes)
        self.bindings: dict[tuple[UUID, str, UUID, str], NotificationSourceBinding] = {}

    def list_routes_for_source_ref(
        self, workspace_id: UUID, source_ref: str, *, for_update: bool = False
    ) -> tuple[NotificationRoute, ...]:
        del for_update
        return tuple(
            item
            for item in self.routes
            if item.workspace_id == workspace_id and item.source_ref == source_ref
        )

    def get_binding(
        self,
        workspace_id: UUID,
        resource_type: str,
        resource_id: UUID,
        binding_kind: str,
    ) -> NotificationSourceBinding | None:
        return self.bindings.get(
            (workspace_id, resource_type, resource_id, binding_kind)
        )

    def add_binding_or_get_existing(
        self, item: NotificationSourceBinding
    ) -> NotificationSourceBinding:
        key = (
            item.workspace_id,
            item.resource_type,
            item.resource_id,
            item.binding_kind.value,
        )
        existing = self.bindings.get(key)
        if existing is not None:
            if existing != item:
                raise ValidationError("notification source binding conflicts")
            return existing
        self.bindings[key] = item
        return item

    def copy_origin_binding(
        self,
        workspace_id: UUID,
        source_resource_type: str,
        source_resource_id: UUID,
        target_resource_type: str,
        target_resource_id: UUID,
    ) -> NotificationSourceBinding | None:
        source = self.get_binding(
            workspace_id,
            source_resource_type,
            source_resource_id,
            NotificationBindingKind.ORIGIN.value,
        )
        if source is None:
            return None
        return self.add_binding_or_get_existing(
            NotificationSourceBinding(
                workspace_id=workspace_id,
                resource_type=target_resource_type,
                resource_id=target_resource_id,
                notification_route_id=source.notification_route_id,
                binding_kind=source.binding_kind,
                bound_at=source.bound_at,
            )
        )


def unit(
    definition: ModuleDefinition,
    instance: ModuleInstance,
    stored_profile: PlanningProfile | None = None,
    stored_request: PlanningRequest | None = None,
    current_proposal: object | None = None,
    extra_definitions: tuple[ModuleDefinition, ...] = (),
    extra_instances: tuple[ModuleInstance, ...] = (),
    notification_routes: tuple[NotificationRoute, ...] = (),
) -> Any:
    return SimpleNamespace(
        tasks=SimpleNamespace(),
        plans=SimpleNamespace(),
        modules=ModuleRepository(
            definition,
            instance,
            extra_definitions=extra_definitions,
            extra_instances=extra_instances,
        ),
        planning_profiles=ProfileRepository(stored_profile),
        planning_requests=RequestRepository(stored_request),
        planning_runs=RunRepository(stored_request),
        planning_leases=LeaseRepository(),
        planning_records=RecordRepository(current_proposal),
        work_contexts=WorkContextRepository(stored_request),
        proposal_materializations=SimpleNamespace(),
        execution_requests=SimpleNamespace(),
        context_assemblies=ContextAssemblyRepository(),
        events=EventRepository(),
        projection_changes=ProjectionPublisher(),
        notifications=NotificationRepository(notification_routes),
    )


def envelope(command_type: str, payload: dict[str, Any], **overrides: object):
    values: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": WORKSPACE_ID,
        "command_type": command_type,
        "target_type": "workspace",
        "actor_type": "local",
        "actor_id": "operator",
        "target_id": None,
        "payload": payload,
        "expected_version": None,
        "idempotency_key": uuid4().hex,
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return CommandEnvelope(**values)  # type: ignore[arg-type]


def register_module_payload() -> dict[str, object]:
    return {
        "provider": "fake",
        "interface_version": "sidera.planner.v1",
        "config_schema_version": 1,
        "capabilities": {
            "structured_output": "json_schema",
            "clarification": True,
            "idempotency": "operation-id",
            "cancellation": "run",
            "usage_reporting": "tokens",
            "max_context_bytes": 128_000,
            "stability": "stable",
            "interface_version": "sidera.planner.v1",
            "proposal_schema_versions": [2],
        },
        "instance_name": "test-planner",
        "config_namespace": "planning",
        "config": {"planning.mode": "test"},
        "secret_refs": [],
    }


def register_profile_payload(instance_id: UUID) -> dict[str, object]:
    return {
        "profile_key": "default",
        "version": 1,
        "display_name": "Default planner",
        "planner_module_instance_id": str(instance_id),
        "planner_module_config_version": 1,
        "planner_interface_version": "sidera.planner.v1",
        "prompt_policy_version": "prompt.v1",
        "system_policy": {"approval": {"required": True}},
        "proposal_schema_version": 2,
        "allowed_capabilities": ["code"],
        "adapter_preferences": ["opencode"],
        "adapter_forbidden": [],
        "budget": {
            "max_context_bytes": 64_000,
            "max_tokens": 10_000,
            "max_cost_microunits": 1_000_000,
            "max_runtime_seconds": 600,
            "max_revisions": 3,
            "max_nodes": 20,
            "max_concurrency": 2,
        },
        "retry_policy": {
            "max_retries": 1,
            "backoff_seconds": [5],
            "unknown_outcome_policy": "block",
            "invalid_output_policy": "retry",
        },
        "default_priority": 50,
        "execution_max_attempts": 2,
        "execution_budget_level": "standard",
        "user_selectable": True,
        "planner_secret_refs": [],
    }


def new_request(
    stored_profile: PlanningProfile,
    *,
    source_type: str = "web",
    source_ref: str | None = "conversation:test",
) -> PlanningRequest:
    return PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_snapshot=stored_profile.snapshot(planner_capabilities()),
        objective="Implement planning",
        constraints=("Keep it reviewable",),
        acceptance_criteria=("The request can be completed",),
        context_refs=("context:test",),
        requested_outputs=("proposal",),
        source_type=source_type,
        source_ref=source_ref,
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )


def waiting_request(stored_profile: PlanningProfile) -> PlanningRequest:
    request = new_request(stored_profile)
    run = request.runs[0]
    lease_id = uuid4()
    request.start_planning(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        now=NOW,
    )
    clarification = PlanningClarification.create(
        clarification_id=uuid4(),
        planning_request_id=request.id,
        planning_run_id=run.id,
        question="Which adapter should be used?",
        rationale="The request allows multiple adapters.",
        answer_type="choice",
        options=("opencode", "dsh"),
        blocking=True,
        created_at=NOW,
    )
    request.mark_needs_clarification(
        run_id=run.id,
        clarifications=(clarification,),
        lease_id=lease_id,
        fencing_token=1,
        now=NOW,
    )
    return request


def proposal_ready_request(stored_profile: PlanningProfile) -> PlanningRequest:
    request = new_request(stored_profile)
    run = request.runs[0]
    lease_id = uuid4()
    request.start_planning(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        now=NOW,
    )
    request.succeed_run(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        provider_ref="fake:proposal-1",
        structured_result_type="planning.proposal.v2",
        usage=run.usage,
        now=NOW,
    )
    request.mark_proposal_ready(
        run_id=run.id,
        proposal_revision=1,
        content_hash=PROPOSAL_HASH,
        now=NOW,
    )
    return request


def blocked_request(stored_profile: PlanningProfile) -> PlanningRequest:
    request = new_request(stored_profile)
    run = request.runs[0]
    lease_id = uuid4()
    request.start_planning(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        now=NOW,
    )
    request.block_run_unknown(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        error_code="provider_unknown_outcome",
        error_message="provider result was not observed",
        now=NOW,
    )
    return request


def native_blocked_request(
    stored_profile: PlanningProfile,
    *,
    started: bool = False,
    error_code: str = "temporary_unavailable",
    retries_used: int = 0,
) -> PlanningRequest:
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_snapshot=stored_profile.snapshot(),
        objective="Implement planning",
        constraints=("Keep it reviewable",),
        acceptance_criteria=("The request can be completed",),
        context_refs=("context:test",),
        requested_outputs=("proposal",),
        source_type="task_goal",
        source_ref="conversation:test",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )
    for attempt in range(retries_used + 1):
        run = request.runs[-1]
        if started:
            lease_id = uuid4()
            request.start_planning(
                run_id=run.id, lease_id=lease_id, fencing_token=1, now=NOW
            )
            request.block_run_unknown(
                run_id=run.id,
                lease_id=lease_id,
                fencing_token=1,
                error_code=error_code,
                error_message="context assembly was blocked",
                now=NOW,
            )
        else:
            request.block_queued_run(
                run_id=run.id,
                error_code=error_code,
                error_message="context assembly was blocked",
                now=NOW,
            )
        if attempt < retries_used:
            request.retry_blocked(new_run_id=uuid4(), now=NOW)
    return request


def native_prestart_context_retry_case(
    *,
    expired: bool = False,
    started: bool = False,
    error_code: str = "temporary_unavailable",
    max_retries: int = 0,
    backoff_seconds: tuple[int, ...] = (),
    invalid_output_policy: InvalidOutputPolicy = InvalidOutputPolicy.BLOCK,
    retries_used: int = 0,
    retry_delay_seconds: int = 10,
    deny_grant: bool = False,
) -> Any:
    definition, instance = module_facts()
    planner_ref = ProfileRef(workspace_id=WORKSPACE_ID, profile_id=uuid4(), revision=3)
    legacy_profile = profile(instance.id)
    stored_profile = replace(
        legacy_profile,
        execution_mode="native_profile",
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_agent_ref=planner_ref,
        retry_policy=replace(
            legacy_profile.retry_policy,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            invalid_output_policy=invalid_output_policy,
        ),
    )
    request = native_blocked_request(
        stored_profile,
        started=started,
        error_code=error_code,
        retries_used=retries_used,
    )
    run = request.runs[-1]
    unit_of_work = unit(definition, instance, stored_profile, request)
    revision = unit_of_work.work_contexts.get_revision(
        unit_of_work.work_contexts.context.current_revision_id, WORKSPACE_ID
    )
    assembly = build_planning_context_assembly_request(
        run=run,
        brief=request.briefs[-1],
        work_context_revision=revision,
        ttl_seconds=1 if expired else 1_800,
        created_at=run.queued_at,
    )
    unit_of_work.context_assemblies.stored = replace(
        assembly,
        status=ContextAssemblyStatus.BLOCKED,
        error_code=error_code,
        lease_owner=None,
        lease_expires_at=None,
        attempt=5,
        version=11,
        updated_at=NOW,
    )
    unit_of_work.context_assemblies.expected_consumer = (
        ContextPurpose.PLANNING_RUN,
        run.id,
        WORKSPACE_ID,
    )
    identity = TrustedAssignmentIdentity("operator", uuid4(), actor_type="local")
    goal = TaskGoalSubmission(
        workspace_id=WORKSPACE_ID,
        root_task_id=UUID("70000000-0000-0000-0000-000000000002"),
        planning_request_id=request.id,
        root_task_version=1,
        principal_id=identity.principal_id,
        grant_id=identity.grant_id,
    )
    goal_reads = []

    def get_goal(workspace_id, request_id):
        assert (workspace_id, request_id) == (WORKSPACE_ID, request.id)
        goal_reads.append((workspace_id, request_id))
        return goal

    unit_of_work.task_goals = SimpleNamespace(get=get_goal, submission=goal)
    expected_target = PlanningCommandTarget(
        workspace_id=WORKSPACE_ID,
        principal_id=identity.principal_id,
        planning_request_id=request.id,
        planning_profile_id=stored_profile.id,
        planning_profile_version=stored_profile.version,
        planner_agent_ref=planner_ref,
        proposal_content_hash=None,
        context_refs=("context:test",),
        root_task_id=goal.root_task_id,
    )
    authorization_targets = []
    resolved_profiles = []

    def require(grant_id, action, target):
        assert grant_id == identity.grant_id
        assert action == "planning.write"
        assert type(target) is PlanningCommandTarget
        assert target == expected_target
        authorization_targets.append(target)
        if deny_grant:
            raise AgentError("permission_denied", "grant denied")

    def resolve_profile(workspace_id, profile_id, revision):
        assert (workspace_id, profile_id, revision) == (
            planner_ref.workspace_id,
            planner_ref.profile_id,
            planner_ref.revision,
        )
        resolved_profiles.append(planner_ref)
        return planner_ref

    def get_link(workspace_id, run_id, *, for_update=False):
        assert (workspace_id, run_id, for_update) == (WORKSPACE_ID, run.id, True)
        return unit_of_work.planning_authorities.link

    unit_of_work.planning_authorities = SimpleNamespace(get_link=get_link, link=None)
    retry_at = NOW + timedelta(seconds=retry_delay_seconds)
    handler = PlanningCommandHandler(
        local_actor_type="local",
        local_actor_id="operator",
        clock=lambda: retry_at,
        planning_context_ttl_seconds=900,
        identity=identity,
        authorizer=SimpleNamespace(require=require),
        assignment_handler=SimpleNamespace(resolve_profile=resolve_profile),
    )
    command = envelope(
        "RetryBlockedPlanningRun",
        {"unknown_outcome_decision": "retry"},
        target_type="planning_run",
        target_id=run.id,
        expected_version=run.version,
    )
    return SimpleNamespace(
        handler=handler,
        unit=unit_of_work,
        request=request,
        run=run,
        profile=stored_profile,
        goal=goal,
        goal_reads=goal_reads,
        authorization_targets=authorization_targets,
        resolved_profiles=resolved_profiles,
        command=command,
        retry_at=retry_at,
        revision=revision,
    )


def native_retry_link(case: Any, status: str) -> PlanningAgentLink:
    authority_id = uuid4()
    start = RunStart(
        operation_id=case.run.operation_id,
        profile_ref=case.profile.planner_agent_ref,
        grant_id=authority_id,
        policy="bounded",
        instructions=(),
        text=case.request.briefs[-1].objective,
        context=(),
        tools=(),
        workspace=ExecutionWorkspace(
            directory="/tmp/sidera-native-prestart-retry-test",
            writable=True,
            artifact_directory=None,
        ),
        required_capabilities=(),
    )
    receipt = None
    if status in {"rejected", "waiting_capacity"}:
        receipt = StartReceipt(
            operation_id=start.operation_id,
            status=StartStatus.REJECTED if status == "rejected" else StartStatus.BUSY,
            run_id=None,
            reason_code="test_not_admitted",
        )
    return PlanningAgentLink(
        workspace_id=WORKSPACE_ID,
        planning_run_id=case.run.id,
        authority_id=authority_id,
        start_operation_id=start.operation_id,
        profile_ref=start.profile_ref,
        start_request=start,
        request_sha256="a" * 64,
        status=status,
        run_id=uuid4() if status in {"running", "cleanup_pending", "closed"} else None,
        start_receipt=receipt,
        closed=status == "closed",
    )


def assert_native_prestart_context_retry_refused(
    case: Any, exception: type[Exception], message: str
) -> None:
    request_before = replace(case.request)
    runs_before = tuple(replace(run) for run in case.request.runs)
    assembly_before = case.unit.context_assemblies.stored
    link_before = case.unit.planning_authorities.link
    with pytest.raises(exception, match=message) as denied:
        case.handler.handle(case.unit, case.command)
    if exception is NativePlanningCleanupPending:
        assert denied.value.code == "native_cleanup_pending"
    assert case.request == request_before
    assert case.request.runs == runs_before
    assert case.unit.planning_requests.added == []
    assert case.unit.planning_requests.saved == []
    assert case.unit.planning_runs.added == []
    assert case.unit.planning_runs.saved == []
    assert case.unit.context_assemblies.added == []
    assert case.unit.context_assemblies.stored is assembly_before
    assert case.unit.planning_authorities.link is link_before
    assert case.unit.events.events == []
    assert case.unit.task_goals.submission is case.goal


def test_register_planner_module_requires_operator_and_rejects_secret_values() -> None:
    definition, instance = module_facts()
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )
    invalid_actor = envelope(
        "RegisterPlannerModule",
        register_module_payload(),
        actor_type="user",
        actor_id="user:1",
    )
    with pytest.raises(ValidationError, match="operator"):
        handler.handle(unit(definition, instance), invalid_actor)

    secret_payload = register_module_payload()
    secret_payload["config"] = {"planning.api_key": "raw-secret"}
    with pytest.raises(ValidationError, match="secret"):
        handler.handle(
            unit(definition, instance),
            envelope("RegisterPlannerModule", secret_payload),
        )


def test_register_planner_module_creates_definition_and_instance() -> None:
    definition, instance = module_facts()
    unit_of_work = unit(definition, instance)
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    result = handler.handle(
        unit_of_work,
        envelope("RegisterPlannerModule", register_module_payload()),
    )

    added_definition, added_instance = unit_of_work.modules.added
    assert result.data["module_definition_id"] == str(added_definition.id)
    assert result.data["module_instance_id"] == str(added_instance.id)
    assert unit_of_work.events.events


def test_register_collaboration_runtime_requires_operator_and_creates_module() -> None:
    definition, instance = module_facts()
    unit_of_work = unit(definition, instance)
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    result = handler.handle(
        unit_of_work,
        envelope(
            "RegisterCollaborationRuntimeModule",
            {
                "provider": "reference",
                "interface_version": "collaboration-runtime.v1",
                "config_schema_version": 1,
                "capabilities": {
                    "interface_version": "collaboration-runtime.v1",
                    "projection_schema_versions": [1],
                    "decision_schema_versions": [1],
                    "strategy_ids": ["all_required"],
                    "idempotency": "operation-id",
                    "max_projection_bytes": 262_144,
                    "max_decision_bytes": 65_536,
                    "max_intents_per_decision": 128,
                    "max_strategy_state_bytes": 16_384,
                    "stability": "stable",
                },
                "instance_name": "sidera-reference",
                "config_namespace": "collaboration",
                "config": {},
                "secret_refs": [],
            },
        ),
    )

    added_definition, added_instance = unit_of_work.modules.added
    assert result.data["module_definition_id"] == str(added_definition.id)
    assert result.data["module_instance_id"] == str(added_instance.id)
    assert added_definition.module_type == COLLABORATION_RUNTIME_MODULE_TYPE
    assert added_definition.provider == "reference"
    assert added_instance.definition_id == added_definition.id
    assert [event.event_type for event in unit_of_work.events.events] == [
        "CollaborationRuntimeModuleRegistered",
        "CollaborationRuntimeModuleInstanceRegistered",
    ]


def test_register_collaboration_runtime_rejects_non_operator_and_raw_secrets() -> None:
    definition, instance = module_facts()
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )
    payload = {
        "provider": "reference",
        "interface_version": "collaboration-runtime.v1",
        "config_schema_version": 1,
        "capabilities": {
            "interface_version": "collaboration-runtime.v1",
            "projection_schema_versions": [1],
            "decision_schema_versions": [1],
            "strategy_ids": ["all_required"],
            "idempotency": "operation-id",
            "max_projection_bytes": 262_144,
            "max_decision_bytes": 65_536,
            "max_intents_per_decision": 128,
            "max_strategy_state_bytes": 16_384,
            "stability": "stable",
        },
        "instance_name": "sidera-reference",
        "config_namespace": "collaboration",
        "config": {"runtime.api_key": "raw-secret"},
        "secret_refs": [],
    }

    with pytest.raises(OperatorRequired, match="operator"):
        handler.handle(
            unit(definition, instance),
            envelope(
                "RegisterCollaborationRuntimeModule",
                {**payload, "config": {}},
                actor_type="user",
                actor_id="user:1",
            ),
        )
    with pytest.raises(ValidationError, match="secret"):
        handler.handle(
            unit(definition, instance),
            envelope("RegisterCollaborationRuntimeModule", payload),
        )


def test_register_planning_profile_validates_module_and_persists_version() -> None:
    definition, instance = module_facts()
    unit_of_work = unit(definition, instance)
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    result = handler.handle(
        unit_of_work,
        envelope(
            "RegisterPlanningProfile",
            register_profile_payload(instance.id),
        ),
    )

    assert result.data["planning_profile_id"] == str(
        unit_of_work.planning_profiles.stored.id
    )
    assert result.data["profile_version"] == 1
    assert (
        unit_of_work.planning_profiles.stored.planner_module_instance_id == instance.id
    )


def test_register_planning_profile_pins_an_active_runtime_by_trusted_references() -> (
    None
):
    planner_definition, planner_instance = module_facts(proposal_schema_versions=(4,))
    runtime_definition_id = uuid4()
    runtime_instance_id = uuid4()
    runtime_capabilities = CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime-v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.EXPERIMENTAL,
    )
    runtime_definition = ModuleDefinition(
        id=runtime_definition_id,
        workspace_id=WORKSPACE_ID,
        module_type=COLLABORATION_RUNTIME_MODULE_TYPE,
        provider="runtime-test",
        interface_version="collaboration-runtime-v1",
        config_schema_version=1,
        capabilities=runtime_capabilities,
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    runtime_instance = ModuleInstance(
        id=runtime_instance_id,
        workspace_id=WORKSPACE_ID,
        definition_id=runtime_definition_id,
        instance_name="test-runtime",
        config_version=3,
        config_namespace="runtime",
        config={"runtime.mode": "test"},
        secret_refs=("secret://runtime/key",),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    unit_of_work = unit(
        planner_definition,
        planner_instance,
        extra_definitions=(runtime_definition,),
        extra_instances=(runtime_instance,),
    )
    payload = register_profile_payload(planner_instance.id)
    payload.update(
        {
            "proposal_schema_version": 4,
            "collaboration_runtime_definition_id": str(runtime_definition_id),
            "collaboration_runtime_instance_id": str(runtime_instance_id),
            "collaboration_runtime_config_version": 3,
        }
    )

    result = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    ).handle(
        unit_of_work,
        envelope("RegisterPlanningProfile", payload),
    )

    binding = unit_of_work.planning_profiles.stored.collaboration_runtime_binding
    assert result.data["profile_version"] == 1
    assert binding is not None
    assert binding.runtime_definition_id == runtime_definition_id
    assert binding.runtime_instance_id == runtime_instance_id
    assert binding.runtime_config_version == 3
    assert "runtime-test" not in repr(binding)
    assert "secret://runtime/key" not in repr(binding)


def test_profile_payload_accepts_only_trusted_collaboration_runtime_references() -> (
    None
):
    payload = register_profile_payload(uuid4())
    payload["proposal_schema_version"] = 4
    payload.update(
        {
            "collaboration_runtime_definition_id": str(uuid4()),
            "collaboration_runtime_instance_id": str(uuid4()),
            "collaboration_runtime_config_version": 3,
        }
    )

    parsed = RegisterPlanningProfilePayload.model_validate(payload)

    assert parsed.collaboration_runtime_config_version == 3

    payload["collaboration_runtime_binding"] = {"api_key": "secret-value"}
    with pytest.raises(ValueError, match="extra"):
        RegisterPlanningProfilePayload.model_validate(payload)


def test_submit_planning_request_returns_request_and_run_ids() -> None:
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    unit_of_work = unit(definition, instance, stored_profile=stored_profile)
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )
    result = handler.handle(
        unit_of_work,
        envelope(
            "SubmitPlanningRequest",
            {
                "profile_id": str(stored_profile.id),
                "profile_version": 1,
                "objective": "Implement planning",
                "constraints": ["Keep it reviewable"],
                "acceptance_criteria": ["The run is queued"],
                "context_refs": ["context:test"],
                "requested_outputs": ["proposal"],
                "source_type": "web",
                "source_ref": "conversation:test",
            },
        ),
    )

    assert result.data["planning_request_id"] == str(result.aggregate_id)
    assert UUID(str(result.data["planning_run_id"]))
    assert result.data["brief_revision"] == 1
    assert unit_of_work.planning_requests.request is not None
    assert (
        unit_of_work.planning_requests.request.status is PlanningRequestStatus.SUBMITTED
    )


def test_submit_private_planning_request_binds_exact_notification_origin() -> None:
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    source_ref = "src_" + "b" * 32
    route = NotificationRoute(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=uuid4(),
        source_ref=source_ref,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )
    unit_of_work = unit(
        definition,
        instance,
        stored_profile=stored_profile,
        notification_routes=(route,),
    )
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    result = handler.handle(
        unit_of_work,
        envelope(
            "SubmitPlanningRequest",
            {
                "profile_id": str(stored_profile.id),
                "profile_version": 1,
                "objective": "Implement planning",
                "constraints": ["Keep it reviewable"],
                "acceptance_criteria": ["The route is preserved"],
                "context_refs": ["context:test"],
                "requested_outputs": ["proposal"],
                "source_type": "akashic_private",
                "source_ref": source_ref,
            },
        ),
    )

    binding = unit_of_work.notifications.get_binding(
        WORKSPACE_ID,
        "planning_request",
        result.aggregate_id,
        NotificationBindingKind.ORIGIN.value,
    )
    assert binding is not None
    assert binding.notification_route_id == route.id


class StubProposalMaterializer:
    def __init__(self, materialization: object) -> None:
        self.materialization = materialization

    def materialize(self, *args: object, **kwargs: object):
        del args, kwargs
        return SimpleNamespace(
            materialization=self.materialization,
            event_ids=(),
            pending_events=(),
            bindings=(),
        )


def test_confirm_initial_proposal_copies_request_origin_to_root() -> None:
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    request = proposal_ready_request(stored_profile)
    source_ref = "src_" + "c" * 32
    request.source_type = "akashic_private"
    request.source_ref = source_ref
    route = NotificationRoute(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=uuid4(),
        source_ref=source_ref,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )
    root_task_id = uuid4()
    materialization = SimpleNamespace(
        root_task_id=root_task_id,
        plan_snapshot_id=uuid4(),
        plan_generation=1,
        subtask_task_mapping={},
        worker_item_mapping={},
        execution_request_mapping={},
        deep_link="/tasks/root",
    )
    proposal = SimpleNamespace(
        planning_request_id=request.id,
        revision=1,
        content_hash=PROPOSAL_HASH,
        planning_run_id=request.current_run_id,
    )
    unit_of_work = unit(
        definition,
        instance,
        stored_profile=stored_profile,
        stored_request=request,
        current_proposal=proposal,
        notification_routes=(route,),
    )
    unit_of_work.notifications.add_binding_or_get_existing(
        NotificationSourceBinding(
            workspace_id=WORKSPACE_ID,
            resource_type="planning_request",
            resource_id=request.id,
            notification_route_id=route.id,
            binding_kind=NotificationBindingKind.ORIGIN,
            bound_at=NOW,
        )
    )
    handler = PlanningCommandHandler(
        local_actor_type="local",
        local_actor_id="operator",
        clock=lambda: NOW,
        materializer=StubProposalMaterializer(materialization),
    )

    handler.handle(
        unit_of_work,
        envelope(
            "ConfirmPlanningProposal",
            {
                "proposal_revision": 1,
                "proposal_content_hash": PROPOSAL_HASH,
                "profile_id": str(stored_profile.id),
                "profile_version": 1,
            },
            target_type="planning_request",
            target_id=request.id,
            expected_version=request.version,
        ),
    )

    binding = unit_of_work.notifications.get_binding(
        WORKSPACE_ID,
        "root_task",
        root_task_id,
        NotificationBindingKind.ORIGIN.value,
    )
    assert binding is not None
    assert binding.notification_route_id == route.id


def test_confirm_initial_proposal_rejects_conflicting_root_origin() -> None:
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    request = proposal_ready_request(stored_profile)
    source_ref = "src_" + "d" * 32
    request.source_type = "akashic_private"
    request.source_ref = source_ref
    request_route = NotificationRoute(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=uuid4(),
        source_ref=source_ref,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )
    root_task_id = uuid4()
    materialization = SimpleNamespace(
        root_task_id=root_task_id,
        plan_snapshot_id=uuid4(),
        plan_generation=1,
        subtask_task_mapping={},
        worker_item_mapping={},
        execution_request_mapping={},
        deep_link="/tasks/root",
    )
    proposal = SimpleNamespace(
        planning_request_id=request.id,
        revision=1,
        content_hash=PROPOSAL_HASH,
        planning_run_id=request.current_run_id,
    )
    unit_of_work = unit(
        definition,
        instance,
        stored_profile=stored_profile,
        stored_request=request,
        current_proposal=proposal,
        notification_routes=(request_route,),
    )
    unit_of_work.notifications.add_binding_or_get_existing(
        NotificationSourceBinding(
            workspace_id=WORKSPACE_ID,
            resource_type="planning_request",
            resource_id=request.id,
            notification_route_id=request_route.id,
            binding_kind=NotificationBindingKind.ORIGIN,
            bound_at=NOW,
        )
    )
    unit_of_work.notifications.add_binding_or_get_existing(
        NotificationSourceBinding(
            workspace_id=WORKSPACE_ID,
            resource_type="root_task",
            resource_id=root_task_id,
            notification_route_id=uuid4(),
            binding_kind=NotificationBindingKind.ORIGIN,
            bound_at=NOW,
        )
    )
    handler = PlanningCommandHandler(
        local_actor_type="local",
        local_actor_id="operator",
        clock=lambda: NOW,
        materializer=StubProposalMaterializer(materialization),
    )

    with pytest.raises(NotificationRouteConflict):
        handler.handle(
            unit_of_work,
            envelope(
                "ConfirmPlanningProposal",
                {
                    "proposal_revision": 1,
                    "proposal_content_hash": PROPOSAL_HASH,
                    "profile_id": str(stored_profile.id),
                    "profile_version": 1,
                },
                target_type="planning_request",
                target_id=request.id,
                expected_version=request.version,
            ),
        )


def test_cancel_planning_request_requires_expected_version_and_updates_request() -> (
    None
):
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_snapshot=stored_profile.snapshot(planner_capabilities()),
        objective="Implement planning",
        constraints=(),
        acceptance_criteria=("The request can be cancelled",),
        context_refs=(),
        requested_outputs=("proposal",),
        source_type="web",
        source_ref=None,
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )
    unit_of_work = unit(definition, instance, stored_profile, request)
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )
    result = handler.handle(
        unit_of_work,
        envelope(
            "CancelPlanningRequest",
            {},
            target_type="planning_request",
            target_id=request.id,
            expected_version=request.version,
        ),
    )

    assert result.aggregate_id == request.id
    assert request.status is PlanningRequestStatus.CANCELLED
    assert unit_of_work.planning_requests.saved[0][1] == 1


def test_answer_planning_clarification_creates_one_new_brief_and_run() -> None:
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    request = waiting_request(stored_profile)
    clarification = request.clarifications[0]
    unit_of_work = unit(definition, instance, stored_profile, request)
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    result = handler.handle(
        unit_of_work,
        envelope(
            "AnswerPlanningClarification",
            {
                "answers": [
                    {
                        "clarification_id": str(clarification.id),
                        "answer": "opencode",
                    }
                ]
            },
            target_type="planning_request",
            target_id=request.id,
            expected_version=request.version,
        ),
    )

    assert result.data["brief_revision"] == 2
    assert result.data["planning_run_id"] == str(request.runs[-1].id)
    assert request.status is PlanningRequestStatus.PLANNING
    assert request.current_brief_revision == 2
    assert len(request.briefs) == 2
    assert len(request.runs) == 2
    assert request.runs[-1].status is PlanningRunStatus.QUEUED
    assert request.clarifications[0].answer == "opencode"
    assert len(unit_of_work.planning_records.briefs) == 1
    assert len(unit_of_work.planning_records.saved_clarifications) == 1


def test_answer_planning_clarification_rejects_partial_answers_without_mutation() -> (
    None
):
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    request = waiting_request(stored_profile)
    unit_of_work = unit(definition, instance, stored_profile, request)
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    with pytest.raises(ValidationError, match="clarification"):
        handler.handle(
            unit_of_work,
            envelope(
                "AnswerPlanningClarification",
                {"answers": []},
                target_type="planning_request",
                target_id=request.id,
                expected_version=request.version,
            ),
        )

    assert request.current_brief_revision == 1
    assert len(request.runs) == 1
    assert unit_of_work.planning_records.briefs == []


def test_request_proposal_revision_preserves_old_proposal_and_appends_brief() -> None:
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    request = proposal_ready_request(stored_profile)
    old_proposal = SimpleNamespace(revision=1, content_hash=PROPOSAL_HASH)
    unit_of_work = unit(
        definition,
        instance,
        stored_profile,
        request,
        current_proposal=old_proposal,
    )
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )
    context = unit_of_work.work_contexts.context
    context_before = replace(context)
    old_revision = unit_of_work.work_contexts.revisions[context.current_revision_id]
    revision_before = replace(old_revision)
    brief_before = request.briefs[0]
    run_before = replace(request.runs[0])
    proposal_before = vars(old_proposal).copy()
    previous_request_version = request.version

    result = handler.handle(
        unit_of_work,
        envelope(
            "RequestProposalRevision",
            {
                "current_proposal_revision": 1,
                "current_proposal_content_hash": PROPOSAL_HASH,
                "feedback": "Split the implementation and verification work.",
            },
            target_type="planning_request",
            target_id=request.id,
            expected_version=request.version,
        ),
    )

    assert result.data["brief_revision"] == 2
    assert request.status is PlanningRequestStatus.PLANNING
    assert request.current_proposal_revision is None
    assert request.current_proposal_content_hash is None
    assert request.briefs[-1].user_feedback == (
        "Split the implementation and verification work."
    )
    assert unit_of_work.planning_records.current_proposal is old_proposal
    assert len(unit_of_work.planning_records.briefs) == 1
    current_revision = unit_of_work.work_contexts.revisions[context.current_revision_id]
    assembly = unit_of_work.context_assemblies.added[0]
    assert current_revision.brief_revision == request.runs[-1].brief_revision == 2
    assert assembly.work_context_revision_id == current_revision.revision_id
    assert current_revision.revision_number == old_revision.revision_number + 1
    assert current_revision.reason is WorkContextRevisionReason.USER_INTERVENTION
    assert current_revision.brief_planning_request_id == request.id
    assert current_revision.proposal_revision_id is None
    assert context.id == context_before.id
    assert (
        context.planning_request_id == context_before.planning_request_id == request.id
    )
    assert context.root_task_id == context_before.root_task_id
    assert context.version == context_before.version + 1
    assert old_revision == revision_before
    assert request.briefs[0] is brief_before
    assert request.runs[0] == run_before
    assert vars(old_proposal) == proposal_before
    assert len(unit_of_work.work_contexts.revisions) == 2
    assert unit_of_work.work_contexts.added == []
    assert unit_of_work.work_contexts.appended == [
        (current_revision, context_before.version)
    ]
    assert unit_of_work.work_contexts.lookups == [
        ("request", request.id, WORKSPACE_ID, True)
    ]
    assert unit_of_work.planning_runs.added == [request.runs[-1]]
    assert unit_of_work.planning_requests.saved == [(request, previous_request_version)]
    assert assembly.consumer_id == request.runs[-1].id
    assert assembly.workspace_id == WORKSPACE_ID
    assert assembly.status is ContextAssemblyStatus.QUEUED
    assert [event.event_type for event in unit_of_work.events.events] == [
        "WorkContextRevised",
        "PlanningProposalRevisionRequested",
    ]
    context_event, event = unit_of_work.events.events
    assert result.event_ids == (context_event.id, event.id)
    assert event.payload == {
        "previous_proposal_revision": 1,
        "previous_proposal_content_hash": PROPOSAL_HASH,
        "brief_revision": 2,
        "planning_run_id": str(request.current_run_id),
    }
    assert result.data == {
        "planning_request_id": str(request.id),
        "planning_run_id": str(request.current_run_id),
        "brief_revision": 2,
        "work_context_id": str(context.id),
        "work_context_revision": 2,
        "work_context_content_hash": current_revision.content_hash,
    }


def test_request_proposal_revision_uses_current_feedback_after_prior_revision() -> None:
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    request = proposal_ready_request(stored_profile)
    unit_of_work = unit(
        definition,
        instance,
        stored_profile,
        request,
        current_proposal=SimpleNamespace(revision=1, content_hash=PROPOSAL_HASH),
    )
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )
    context = unit_of_work.work_contexts.context
    old_revision = unit_of_work.work_contexts.revisions[context.current_revision_id]
    old_revision_before = replace(old_revision)
    old_brief = request.briefs[0]
    old_proposal = unit_of_work.planning_records.current_proposal
    old_proposal_before = vars(old_proposal).copy()

    handler.handle(
        unit_of_work,
        envelope(
            "RequestProposalRevision",
            {
                "current_proposal_revision": 1,
                "current_proposal_content_hash": PROPOSAL_HASH,
                "feedback": "Split implementation and verification.",
            },
            target_type="planning_request",
            target_id=request.id,
            expected_version=request.version,
        ),
    )
    second_revision = unit_of_work.work_contexts.revisions[context.current_revision_id]
    assert second_revision.brief_revision == request.runs[-1].brief_revision == 2
    assert second_revision.revision_number == 2
    assert unit_of_work.context_assemblies.added[0].work_context_revision_id == (
        second_revision.revision_id
    )
    second_revision_before = replace(second_revision)
    second_brief = request.briefs[-1]

    second_run = request.runs[-1]
    second_lease_id = uuid4()
    second_proposal_hash = "b" * 64
    request.start_planning(
        run_id=second_run.id,
        lease_id=second_lease_id,
        fencing_token=second_run.attempt,
        now=NOW,
    )
    request.succeed_run(
        run_id=second_run.id,
        lease_id=second_lease_id,
        fencing_token=second_run.attempt,
        provider_ref="fake:proposal-2",
        structured_result_type="planning.proposal.v2",
        usage=second_run.usage,
        now=NOW,
    )
    request.mark_proposal_ready(
        run_id=second_run.id,
        proposal_revision=2,
        content_hash=second_proposal_hash,
        now=NOW,
    )
    unit_of_work.planning_records.current_proposal = SimpleNamespace(
        revision=2, content_hash=second_proposal_hash
    )
    second_proposal = unit_of_work.planning_records.current_proposal
    second_proposal_before = vars(second_proposal).copy()
    second_run_before = replace(second_run)

    result = handler.handle(
        unit_of_work,
        envelope(
            "RequestProposalRevision",
            {
                "current_proposal_revision": 2,
                "current_proposal_content_hash": second_proposal_hash,
                "feedback": "Require an automated CSV behavior check.",
            },
            target_type="planning_request",
            target_id=request.id,
            expected_version=request.version,
        ),
    )

    assert result.data["brief_revision"] == 3
    assert request.briefs[-1].user_feedback == (
        "Require an automated CSV behavior check."
    )
    assert request.runs[-1].brief_revision == 3
    third_revision = unit_of_work.work_contexts.revisions[context.current_revision_id]
    assert third_revision.brief_revision == 3
    assert third_revision.revision_number == 3
    assert third_revision.revision_id not in {
        old_revision.revision_id,
        second_revision.revision_id,
    }
    assert unit_of_work.context_assemblies.added[1].work_context_revision_id == (
        third_revision.revision_id
    )
    assert unit_of_work.work_contexts.revisions[old_revision.revision_id] == (
        old_revision_before
    )
    assert unit_of_work.work_contexts.revisions[second_revision.revision_id] == (
        second_revision_before
    )
    assert request.briefs[:2] == (old_brief, second_brief)
    assert second_brief.user_feedback == "Split implementation and verification."
    assert request.runs[1] == second_run_before
    assert vars(old_proposal) == old_proposal_before
    assert vars(second_proposal) == second_proposal_before
    assert unit_of_work.planning_records.current_proposal is second_proposal
    assert len(request.briefs) == len(request.runs) == 3
    assert len(unit_of_work.work_contexts.revisions) == 3
    assert len(unit_of_work.work_contexts.appended) == 2
    assert len(unit_of_work.context_assemblies.added) == 2
    assert unit_of_work.planning_records.briefs == [second_brief, request.briefs[-1]]


def _proposal_context_case(*, revision: bool = False, native: bool = False) -> Any:
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )
    if revision:
        unit_of_work, root, snapshot, stored_profile = _revision_fixture()
        handler.handle(
            unit_of_work, _submit_revision_envelope(root, snapshot, stored_profile)
        )
        request = unit_of_work.planning_requests.request
        unit_of_work.planning_runs.request = request
    else:
        definition, instance = module_facts()
        stored_profile = profile(instance.id)
        if native:
            stored_profile = replace(
                stored_profile,
                execution_mode="native_profile",
                planner_module_instance_id=None,
                planner_module_config_version=None,
                planner_agent_ref=ProfileRef(
                    workspace_id=WORKSPACE_ID, profile_id=uuid4(), revision=3
                ),
            )
            request = PlanningRequest.create(
                request_id=uuid4(),
                workspace_id=WORKSPACE_ID,
                profile_snapshot=stored_profile.snapshot(),
                objective="Implement planning",
                constraints=("Keep it reviewable",),
                acceptance_criteria=("The request can be completed",),
                context_refs=("context:test",),
                requested_outputs=("proposal",),
                source_type="web",
                source_ref="conversation:test",
                actor="operator",
                correlation_id=uuid4(),
                run_id=uuid4(),
                now=NOW,
            )
        else:
            request = new_request(stored_profile)
        unit_of_work = unit(definition, instance, stored_profile, request)
    run = request.runs[-1]
    lease_id = uuid4()
    request.start_planning(
        run_id=run.id, lease_id=lease_id, fencing_token=run.attempt, now=NOW
    )
    request.succeed_run(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=run.attempt,
        provider_ref="fake:proposal-1",
        structured_result_type=f"planning.proposal.v{stored_profile.proposal_schema_version}",
        usage=run.usage,
        now=NOW,
    )
    request.mark_proposal_ready(
        run_id=run.id, proposal_revision=1, content_hash=PROPOSAL_HASH, now=NOW
    )
    proposal = SimpleNamespace(
        id=uuid4(),
        planning_request_id=request.id,
        planning_run_id=run.id,
        revision=1,
        content_hash=PROPOSAL_HASH,
    )
    unit_of_work.planning_records.current_proposal = proposal
    if native:
        identity = TrustedAssignmentIdentity("operator", uuid4(), actor_type="local")
        planner_ref = stored_profile.planner_agent_ref

        def get_goal(workspace_id, planning_request_id):
            assert (workspace_id, planning_request_id) == (WORKSPACE_ID, request.id)

        def require(grant_id, action, target):
            assert grant_id == identity.grant_id
            assert action == "planning.write"
            assert target == PlanningCommandTarget(
                workspace_id=WORKSPACE_ID,
                principal_id="operator",
                planning_request_id=request.id,
                planning_profile_id=stored_profile.id,
                planning_profile_version=stored_profile.version,
                planner_agent_ref=planner_ref,
                proposal_content_hash=None,
                context_refs=("context:test",),
            )

        def resolve_profile(workspace_id, profile_id, revision):
            assert (workspace_id, profile_id, revision) == (
                WORKSPACE_ID,
                planner_ref.profile_id,
                planner_ref.revision,
            )
            return planner_ref

        unit_of_work.task_goals = SimpleNamespace(get=get_goal)
        handler = PlanningCommandHandler(
            local_actor_type="local",
            local_actor_id="operator",
            clock=lambda: NOW,
            identity=identity,
            authorizer=SimpleNamespace(require=require),
            assignment_handler=SimpleNamespace(resolve_profile=resolve_profile),
        )
    return SimpleNamespace(
        unit=unit_of_work,
        handler=handler,
        request=request,
        proposal=proposal,
        command=envelope(
            "RequestProposalRevision",
            {
                "current_proposal_revision": 1,
                "current_proposal_content_hash": PROPOSAL_HASH,
                "feedback": "Split implementation and verification.",
            },
            target_type="planning_request",
            target_id=request.id,
            expected_version=request.version,
        ),
    )


def test_proposal_context_carries_typed_facts_and_derives_semantics_from_new_brief() -> (
    None
):
    case = _proposal_context_case()
    context = case.unit.work_contexts.context
    root_id = uuid4()
    context.bind_root_task(root_id, expected_version=context.version, now=NOW)
    old_revision = case.unit.work_contexts.revisions[context.current_revision_id]
    rich_snapshot = replace(
        old_revision.snapshot,
        objective="Context-only objective",
        constraints=(ContextTextItem("old-constraint", "Context-only constraint", 0),),
        acceptance_criteria=(ContextTextItem("old-check", "Context-only check", 0),),
        requested_outputs=(ContextTextItem("old-output", "Context-only output", 0),),
        clarification_decisions=(
            ClarificationDecision(
                "adapter", "Which adapter?", "opencode", "operator", 0
            ),
        ),
        accepted_decisions=(
            AcceptedDecision("review", "Keep review gates", "user", 0),
        ),
        unresolved_questions=(
            UnresolvedQuestion("owner", "Who owns delivery?", True, 0),
        ),
        knowledge_refs=(
            KnowledgeRevisionRef(
                page_id=uuid4(),
                revision_id=uuid4(),
                revision_hash="b" * 64,
                section_key="overview",
                section_hash="c" * 64,
                source_ids=(uuid4(),),
                ordinal=0,
            ),
        ),
        artifact_refs=(WorkReference(uuid4(), "artifact", 0),),
        evidence_refs=(WorkReference(uuid4(), "evidence", 0),),
        checkpoint_refs=(WorkReference(uuid4(), "checkpoint", 0),),
        missing_fact_codes=("missing-operator-fact",),
    )
    old_revision = replace(
        old_revision,
        snapshot=rich_snapshot,
        content_hash=rich_snapshot.content_hash,
        plan_snapshot_id=uuid4(),
        proposal_revision_id=uuid4(),
    )
    case.unit.work_contexts.revisions[old_revision.revision_id] = old_revision
    old_revision_before = replace(old_revision)

    result = case.handler.handle(case.unit, case.command)

    current = case.unit.work_contexts.revisions[context.current_revision_id]
    assert current.brief_revision == 2
    assert current.revision_number == old_revision.revision_number + 1
    assert current.snapshot.objective == "Implement planning"
    assert tuple(item.text for item in current.snapshot.constraints) == (
        "Keep it reviewable",
    )
    assert tuple(item.text for item in current.snapshot.acceptance_criteria) == (
        "The request can be completed",
    )
    assert tuple(item.text for item in current.snapshot.requested_outputs) == (
        "proposal",
    )
    for field in (
        "clarification_decisions",
        "accepted_decisions",
        "unresolved_questions",
        "knowledge_refs",
        "artifact_refs",
        "evidence_refs",
        "checkpoint_refs",
        "missing_fact_codes",
    ):
        assert getattr(current.snapshot, field) == getattr(rich_snapshot, field)
        assert getattr(current.snapshot, field)
    assert current.plan_snapshot_id == old_revision.plan_snapshot_id
    assert current.proposal_revision_id is None
    assert old_revision == old_revision_before
    assert case.unit.work_contexts.revisions[old_revision.revision_id] is old_revision
    assert context.planning_request_id == case.request.id
    assert context.root_task_id == root_id
    assert result.data["work_context_revision"] == 2
    assert (
        tuple(
            KnowledgeRevisionRef.from_mapping(value)
            for value in case.unit.context_assemblies.added[-1].knowledge_filter_refs
        )
        == rich_snapshot.knowledge_refs
    )


def test_proposal_context_revision_feedback_reuses_exact_retained_root_lineage() -> (
    None
):
    case = _proposal_context_case(revision=True)
    context = case.unit.work_contexts.context
    context_before = replace(context)
    old_revision = case.unit.work_contexts.revisions[context.current_revision_id]
    old_revision_before = replace(old_revision)
    request_before = replace(
        case.request, runs=tuple(replace(run) for run in case.request.runs)
    )
    proposal_before = vars(case.proposal).copy()
    base = case.request.base_descriptor
    old_assembly = case.unit.context_assemblies.added[-1]
    case.unit.work_contexts.lookups.clear()

    result = case.handler.handle(case.unit, case.command)

    current = case.unit.work_contexts.revisions[context.current_revision_id]
    assembly = case.unit.context_assemblies.added[-1]
    assert current.brief_revision == case.request.runs[-1].brief_revision == 2
    assert current.revision_number == old_revision.revision_number + 1 == 3
    assert current.reason is WorkContextRevisionReason.REPLANNED
    assert current.brief_planning_request_id == case.request.id
    assert context.planning_request_id == context_before.planning_request_id
    assert context.planning_request_id != case.request.id
    assert context.id == context_before.id
    assert context.root_task_id == context_before.root_task_id == base.root_task_id
    assert context.version == context_before.version + 1
    assert case.unit.work_contexts.added == []
    assert case.unit.work_contexts.appended[-1] == (current, context_before.version)
    assert case.unit.work_contexts.lookups == [
        ("root", base.root_task_id, WORKSPACE_ID, True)
    ]
    assert (
        current.plan_snapshot_id
        == old_revision.plan_snapshot_id
        == base.plan_snapshot_id
    )
    assert current.proposal_revision_id is None
    assert old_revision == old_revision_before
    assert case.request.base_descriptor == base
    assert case.request.briefs[0] == request_before.briefs[0]
    assert case.request.runs[0] == request_before.runs[0]
    assert vars(case.proposal) == proposal_before
    assert assembly.work_context_revision_id == current.revision_id
    assert assembly.budget["revision_base"] == old_assembly.budget["revision_base"]
    assert assembly.budget["planning_kind"] == "revision"
    assert assembly.consumer_id == case.request.runs[-1].id
    assert result.data["work_context_id"] == str(context.id)
    assert result.data["work_context_revision"] == 3
    context_event, event = case.unit.events.events[-2:]
    assert context_event.event_type == "WorkContextRevised"
    assert event.event_type == "PlanningProposalRevisionRequested"
    assert result.event_ids == (context_event.id, event.id)


def _assert_proposal_context_refused(
    case: Any, error: type[Exception], message: str
) -> None:
    request_before = replace(
        case.request, runs=tuple(replace(run) for run in case.request.runs)
    )
    context_before = (
        None
        if case.unit.work_contexts.context is None
        else replace(case.unit.work_contexts.context)
    )
    revisions_before = case.unit.work_contexts.revisions.copy()
    proposal_before = vars(case.proposal).copy()
    stores = (
        (case.unit.planning_requests, "added"),
        (case.unit.planning_requests, "saved"),
        (case.unit.planning_records, "briefs"),
        (case.unit.planning_runs, "added"),
        (case.unit.planning_runs, "saved"),
        (case.unit.work_contexts, "added"),
        (case.unit.work_contexts, "appended"),
        (case.unit.context_assemblies, "added"),
        (case.unit.events, "events"),
    )
    writes_before = [list(getattr(store, field)) for store, field in stores]

    with pytest.raises(error, match=message):
        case.handler.handle(case.unit, case.command)

    assert case.request == request_before
    assert case.unit.work_contexts.context == context_before
    assert case.unit.work_contexts.revisions == revisions_before
    assert vars(case.proposal) == proposal_before
    assert [getattr(store, field) for store, field in stores] == writes_before


@pytest.mark.parametrize("revision", [False, True], ids=["create", "revision"])
@pytest.mark.parametrize(
    "missing", ["context", "current-revision", "archived", "wrong-scope"]
)
def test_proposal_context_unavailable_rejects_before_request_mutation(
    revision: bool,
    missing: str,
) -> None:
    case = _proposal_context_case(revision=revision)
    context = case.unit.work_contexts.context
    if missing == "context":
        case.unit.work_contexts.context = None
    elif missing == "current-revision":
        del case.unit.work_contexts.revisions[context.current_revision_id]
    elif missing == "archived":
        context.archive(expected_version=context.version, now=NOW)
    elif revision:
        context.root_task_id = uuid4()
    else:
        context.planning_request_id = uuid4()
        context.root_task_id = uuid4()
    error = ValidationError if missing == "archived" else NotFound
    message = "active|archived" if missing == "archived" else "work context"

    _assert_proposal_context_refused(case, error, message)


@pytest.mark.parametrize("revision", [False, True], ids=["create", "revision"])
@pytest.mark.parametrize(
    "invalid", ["version", "proposal-hash", "proposal-revision", "workspace"]
)
def test_proposal_context_feedback_keeps_existing_scope_and_conflict_guards(
    revision: bool,
    invalid: str,
) -> None:
    case = _proposal_context_case(revision=revision)
    if invalid == "version":
        case.command = replace(case.command, expected_version=case.request.version - 1)
    elif invalid == "workspace":
        case.command = replace(case.command, workspace_id=uuid4())
    else:
        payload = dict(case.command.payload)
        if invalid == "proposal-hash":
            payload["current_proposal_content_hash"] = "b" * 64
        else:
            payload["current_proposal_revision"] = 2
        case.command = replace(case.command, payload=payload)
    error = NotFound if invalid == "workspace" else PlanningConflict
    message = "planning request not found" if invalid == "workspace" else "changed"

    _assert_proposal_context_refused(case, error, message)


def test_proposal_context_feedback_reaches_real_worker_and_strict_source_reader() -> (
    None
):
    case = _proposal_context_case(native=True)
    context = case.unit.work_contexts.context
    old_revision = case.unit.work_contexts.revisions[context.current_revision_id]
    case.handler.handle(case.unit, case.command)
    assembly = case.unit.context_assemblies.added[-1]

    def unexpected_io(*args, **kwargs):
        raise AssertionError(
            "proposal context regression must not perform external I/O"
        )

    catalog = SimpleNamespace(materialize=unexpected_io, search=unexpected_io)
    worker = ContextPackageWorker(uow_factory=unexpected_io, catalog=catalog)
    produced_facts = worker._planning_facts(case.unit, assembly)
    assert produced_facts["brief"]["revision"] == 2
    assert (
        produced_facts["brief"]["user_feedback"]
        == "Split implementation and verification."
    )
    new_revision = case.unit.work_contexts.get_revision(
        assembly.work_context_revision_id, WORKSPACE_ID
    )
    manifest = ContextPackageManifestV1.create(
        package_id=uuid4(),
        workspace_id=assembly.workspace_id,
        purpose=assembly.purpose,
        consumer_type=assembly.consumer_type,
        consumer_id=assembly.consumer_id,
        worker_item_id=assembly.worker_item_id,
        work_context_revision_id=new_revision.revision_id,
        context_version=assembly.context_version,
        control_plane_fact_snapshot=produced_facts,
        knowledge_refs=(),
        artifact_refs=new_revision.snapshot.artifact_refs,
        evidence_refs=new_revision.snapshot.evidence_refs,
        budget=ContextBudgetV1(
            profile_id=UUID(str(assembly.budget["profile_id"])),
            profile_version=assembly.budget["profile_version"],
            max_sections=assembly.budget["max_sections"],
            max_chars=assembly.budget["max_chars"],
            max_input_tokens=assembly.budget["max_input_tokens"],
        ),
        expires_at=assembly.expires_at,
        idempotency_key=assembly.idempotency_key,
        created_at=assembly.created_at,
    )
    ready_assembly = replace(assembly, status=ContextAssemblyStatus.READY)

    def get_assembly(purpose, consumer_id, workspace_id):
        assert (purpose, consumer_id, workspace_id) == (
            ContextPurpose.PLANNING_RUN,
            case.request.runs[-1].id,
            WORKSPACE_ID,
        )
        return ready_assembly

    consumer_unit = SimpleNamespace(
        work_contexts=case.unit.work_contexts,
        context_assemblies=SimpleNamespace(get_for_consumer=get_assembly),
    )
    reader = PlanningContextSourceReader(
        unexpected_io,
        authorizer=SimpleNamespace(require=unexpected_io),
        materializer=ContextMaterializer(catalog),
        clock=lambda: NOW,
    )
    validated = reader._facts(
        consumer_unit,
        case.request.runs[-1],
        case.request,
        manifest,
        new_revision,
        include_bodies=False,
    )
    assert validated.revision.brief_revision == 2
    assert validated.manifest.control_plane_fact_snapshot["brief"]["user_feedback"] == (
        "Split implementation and verification."
    )
    assert validated.project_knowledge == ()
    assert validated.result_inputs == ()
    with pytest.raises(AgentError) as denied:
        reader._facts(
            consumer_unit,
            case.request.runs[-1],
            case.request,
            manifest,
            case.unit.work_contexts.get_revision(
                old_revision.revision_id, WORKSPACE_ID
            ),
            include_bodies=False,
        )
    assert denied.value.code == "permission_denied"
    assert old_revision.brief_revision == 1


def test_reject_planning_proposal_records_reason_without_deleting_proposal() -> None:
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    request = proposal_ready_request(stored_profile)
    old_proposal = SimpleNamespace(revision=1, content_hash=PROPOSAL_HASH)
    unit_of_work = unit(
        definition,
        instance,
        stored_profile,
        request,
        current_proposal=old_proposal,
    )
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    result = handler.handle(
        unit_of_work,
        envelope(
            "RejectPlanningProposal",
            {
                "current_proposal_revision": 1,
                "current_proposal_content_hash": PROPOSAL_HASH,
                "reason": "The proposed scope is too broad.",
            },
            target_type="planning_request",
            target_id=request.id,
            expected_version=request.version,
        ),
    )

    assert result.data["status"] == "rejected"
    assert request.status is PlanningRequestStatus.REJECTED
    assert unit_of_work.planning_records.current_proposal is old_proposal
    assert unit_of_work.events.events[-1].payload["reason"] == (
        "The proposed scope is too broad."
    )


def test_retry_blocked_planning_run_requires_operator_and_records_decision() -> None:
    definition, instance = module_facts()
    stored_profile = profile(instance.id)
    request = blocked_request(stored_profile)
    old_run = request.runs[-1]
    unit_of_work = unit(definition, instance, stored_profile, request)
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    unauthorized = envelope(
        "RetryBlockedPlanningRun",
        {"unknown_outcome_decision": "retry"},
        actor_type="user",
        actor_id="user:1",
        target_type="planning_run",
        target_id=old_run.id,
        expected_version=old_run.version,
    )
    with pytest.raises(OperatorRequired):
        handler.handle(unit_of_work, unauthorized)

    result = handler.handle(
        unit_of_work,
        envelope(
            "RetryBlockedPlanningRun",
            {"unknown_outcome_decision": "retry"},
            target_type="planning_run",
            target_id=old_run.id,
            expected_version=old_run.version,
        ),
    )

    new_run = request.runs[-1]
    assert result.data["planning_run_id"] == str(new_run.id)
    assert result.data["unknown_outcome_decision"] == "retry"
    assert request.status is PlanningRequestStatus.PLANNING
    assert new_run.attempt == old_run.attempt + 1
    assert new_run.operation_id != old_run.operation_id
    assert unit_of_work.planning_runs.added == [new_run]
    assert unit_of_work.events.events[-1].payload["unknown_outcome_decision"] == "retry"


@pytest.mark.parametrize("expired", [False, True], ids=["live", "expired"])
@pytest.mark.parametrize(
    "policy_values",
    [
        pytest.param({}, id="original-cap0-empty-backoff-block"),
        pytest.param(
            {
                "max_retries": 1,
                "backoff_seconds": (30,),
                "invalid_output_policy": InvalidOutputPolicy.RETRY,
                "retry_delay_seconds": 2,
            },
            id="bounded-before-model-backoff",
        ),
    ],
)
def test_native_prestart_context_retry_preserves_original_and_queues_fresh_context(
    expired: bool, policy_values: dict[str, Any]
) -> None:
    case = native_prestart_context_retry_case(expired=expired, **policy_values)
    request_before = replace(case.request)
    old_run = case.run
    run_before = replace(old_run)
    old_assembly = case.unit.context_assemblies.stored
    assembly_before = replace(old_assembly)
    goal_before = replace(case.goal)
    assert old_run.started_at is None and old_run.lease_id is None
    assert old_run.fencing_token == 0
    assert old_run.provider_ref is None and old_run.structured_result_type is None
    assert old_run.capability_snapshot is None
    assert case.request.root_task_id is None
    assert case.request.source_type == "task_goal"
    assert (old_assembly.expires_at <= case.retry_at) is expired
    assert old_assembly.status is ContextAssemblyStatus.BLOCKED
    assert old_assembly.error_code == old_run.error_code == "temporary_unavailable"
    assert old_assembly.attempt == 5 and old_assembly.version == 11
    if not policy_values:
        assert old_run.profile_snapshot.retry_policy == PlanningRetryPolicy(
            max_retries=0,
            backoff_seconds=(),
            invalid_output_policy=InvalidOutputPolicy.BLOCK,
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
        )

    result = case.handler.handle(case.unit, case.command)

    assert len(case.request.runs) == 2
    new_run = case.request.runs[-1]
    assert case.request.runs[0] is old_run and old_run == run_before
    assert old_run.status is PlanningRunStatus.BLOCKED
    assert case.unit.context_assemblies.stored is old_assembly
    assert old_assembly == assembly_before
    assert case.unit.task_goals.submission is case.goal and case.goal == goal_before
    assert case.goal_reads == [(WORKSPACE_ID, case.request.id)]
    assert len(case.authorization_targets) == 1
    assert case.authorization_targets[0].root_task_id == case.goal.root_task_id
    assert case.authorization_targets[0].planner_agent_ref == (
        old_run.profile_snapshot.planner_agent_ref
    )
    assert case.resolved_profiles == [case.profile.planner_agent_ref]
    assert case.request.id == request_before.id
    assert case.request.root_task_id is None
    assert case.request.briefs[:-1] == request_before.briefs
    assert case.request.briefs[-1] is request_before.briefs[-1]
    assert case.request.current_brief_revision == request_before.current_brief_revision
    assert case.unit.planning_records.briefs == []
    assert case.request.profile_snapshot == request_before.profile_snapshot
    assert case.request.source_type == request_before.source_type
    assert case.request.source_ref == request_before.source_ref
    assert case.request.correlation_id == request_before.correlation_id
    assert case.request.actor == request_before.actor
    assert case.request.version == request_before.version + 1
    assert case.request.status is PlanningRequestStatus.PLANNING
    assert new_run.id != old_run.id
    assert new_run.planning_request_id == case.request.id
    assert new_run.workspace_id == WORKSPACE_ID
    assert new_run.brief_revision == old_run.brief_revision
    assert new_run.attempt == old_run.attempt + 1
    assert new_run.operation_id != old_run.operation_id
    assert new_run.profile_snapshot == old_run.profile_snapshot
    assert (
        new_run.profile_snapshot.retry_policy == old_run.profile_snapshot.retry_policy
    )
    assert new_run.planner_module_instance_id is None
    assert new_run.planner_module_config_version is None
    assert new_run.capability_snapshot is None
    assert new_run.status is PlanningRunStatus.QUEUED
    assert new_run.queued_at == case.retry_at
    assert new_run.started_at is None and new_run.finished_at is None
    assert new_run.lease_id is None and new_run.fencing_token == 0
    assert new_run.provider_ref is None and new_run.structured_result_type is None
    assert new_run.error_code is None
    assert new_run.usage.input_tokens is None and new_run.usage.cost_microunits is None
    assert case.unit.planning_runs.added == [new_run]
    assert case.unit.planning_runs.saved == []
    assert case.unit.planning_requests.added == []
    assert case.unit.planning_requests.saved == [(case.request, request_before.version)]
    assert case.unit.planning_profiles.stored is case.profile
    assert case.unit.planning_profiles.added == []
    assert len(case.unit.context_assemblies.added) == 1
    fresh_context = case.unit.context_assemblies.added[0]
    assert fresh_context.request_id != old_assembly.request_id
    assert fresh_context.workspace_id == WORKSPACE_ID
    assert fresh_context.purpose is ContextPurpose.PLANNING_RUN
    assert fresh_context.consumer_type is ContextConsumerType.PLANNER
    assert fresh_context.consumer_id == new_run.id
    assert fresh_context.work_context_revision_id == case.revision.revision_id
    assert fresh_context.created_at == fresh_context.updated_at == case.retry_at
    assert fresh_context.expires_at == case.retry_at + timedelta(seconds=900)
    assert fresh_context.status is ContextAssemblyStatus.QUEUED
    assert fresh_context.attempt == fresh_context.version == 1
    assert fresh_context.error_code is None
    assert fresh_context.lease_owner is None and fresh_context.lease_expires_at is None
    assert fresh_context.idempotency_key == f"context:planning-run:{new_run.id}:v1"
    assert fresh_context.request_fingerprint != old_assembly.request_fingerprint
    assert fresh_context.budget == old_assembly.budget
    assert fresh_context.budget["planner_agent_ref"] == {
        "workspace_id": str(WORKSPACE_ID),
        "profile_id": str(case.profile.planner_agent_ref.profile_id),
        "revision": 3,
    }
    assert "planner_module_instance_id" not in fresh_context.budget
    assert "planner_module_config_version" not in fresh_context.budget
    assert case.unit.context_assemblies.reads == [
        (ContextPurpose.PLANNING_RUN, old_run.id, WORKSPACE_ID, True)
    ]
    assert len(case.unit.events.events) == 1
    event = case.unit.events.events[0]
    assert event.workspace_id == WORKSPACE_ID
    assert event.aggregate_type == "planning_request"
    assert event.aggregate_id == case.request.id
    assert event.aggregate_version == case.request.version
    assert event.event_type == "PlanningBlockedRunRetried"
    assert event.payload == {
        "blocked_run_id": str(old_run.id),
        "planning_run_id": str(new_run.id),
        "attempt": 2,
        "unknown_outcome_decision": "retry",
    }
    assert event.causation_id == case.command.id
    assert result == CommandResult(
        command_id=case.command.id,
        aggregate_id=case.request.id,
        aggregate_version=case.request.version,
        event_ids=(event.id,),
        data={
            "planning_request_id": str(case.request.id),
            "planning_run_id": str(new_run.id),
            "attempt": 2,
            "unknown_outcome_decision": "retry",
        },
    )


@pytest.mark.parametrize(
    "assembly_changes",
    [
        pytest.param(None, id="missing"),
        pytest.param({"workspace_id": uuid4()}, id="foreign-workspace"),
        pytest.param({"consumer_id": uuid4()}, id="foreign-consumer"),
        pytest.param(
            {
                "purpose": ContextPurpose.EXECUTION,
                "consumer_type": ContextConsumerType.WORKER,
                "worker_item_id": uuid4(),
                "context_version": 1,
            },
            id="foreign-purpose-and-type",
        ),
        *[
            pytest.param({"status": status}, id=f"status-{status.value}")
            for status in ContextAssemblyStatus
            if status is not ContextAssemblyStatus.BLOCKED
        ],
        pytest.param({"error_code": None}, id="missing-error"),
        pytest.param({"error_code": "context_assembly_blocked"}, id="mismatched-error"),
        pytest.param({"lease_owner": "assembly-worker"}, id="lease-owner"),
        pytest.param(
            {"lease_expires_at": NOW + timedelta(minutes=1)}, id="lease-expiry"
        ),
        pytest.param(
            {
                "lease_owner": "assembly-worker",
                "lease_expires_at": NOW + timedelta(minutes=1),
            },
            id="active-lease",
        ),
    ],
)
def test_native_prestart_context_retry_refuses_unproven_assembly(
    assembly_changes: dict[str, Any] | None,
) -> None:
    case = native_prestart_context_retry_case()
    case.unit.context_assemblies.stored = (
        None
        if assembly_changes is None
        else replace(case.unit.context_assemblies.stored, **assembly_changes)
    )
    assert_native_prestart_context_retry_refused(
        case, NativePlanningCleanupPending, "original native planning boundary"
    )


@pytest.mark.parametrize(
    "run_changes",
    [
        pytest.param({"started_at": NOW}, id="started"),
        pytest.param({"lease_id": uuid4()}, id="leased"),
        pytest.param({"fencing_token": 1}, id="fenced"),
        pytest.param({"provider_ref": "observed-provider-result"}, id="provider"),
        pytest.param({"structured_result_type": "planning.proposal.v2"}, id="result"),
    ],
)
def test_native_prestart_context_retry_refuses_run_admission_evidence(
    run_changes: dict[str, Any],
) -> None:
    case = native_prestart_context_retry_case()
    case.run = replace(case.run, **run_changes)
    case.request.runs = (case.run,)
    assert_native_prestart_context_retry_refused(
        case, NativePlanningCleanupPending, "original native planning boundary"
    )
    assert case.unit.context_assemblies.reads == []


@pytest.mark.parametrize(
    "error_code",
    [
        "context_assembly_blocked",
        "context_package_expired",
        "native_admission_unknown",
        "provider_unknown_outcome",
        "timeout",
        "invalid_planner_output",
    ],
)
def test_native_prestart_context_retry_refuses_unclassified_prestart_error(
    error_code: str,
) -> None:
    case = native_prestart_context_retry_case(error_code=error_code)
    assert_native_prestart_context_retry_refused(
        case, NativePlanningCleanupPending, "original native planning boundary"
    )
    assert case.unit.context_assemblies.reads == []


@pytest.mark.parametrize(
    "link_status", ["pending", "starting", "running", "unknown", "cleanup_pending"]
)
def test_native_prestart_context_retry_refuses_original_open_native_link(
    link_status: str,
) -> None:
    case = native_prestart_context_retry_case()
    case.unit.planning_authorities.link = native_retry_link(case, link_status)
    assert_native_prestart_context_retry_refused(
        case, NativePlanningCleanupPending, "original native planning boundary"
    )
    assert case.unit.context_assemblies.reads == []


@pytest.mark.parametrize("link_status", [None, "unknown"], ids=["missing", "unknown"])
def test_native_prestart_context_retry_keeps_started_native_run_guard(
    link_status: str | None,
) -> None:
    case = native_prestart_context_retry_case(started=True)
    assert case.run.started_at == NOW
    assert case.run.lease_id is not None and case.run.fencing_token == 1
    if link_status is not None:
        case.unit.planning_authorities.link = native_retry_link(case, link_status)
    assert_native_prestart_context_retry_refused(
        case, NativePlanningCleanupPending, "original native planning boundary"
    )


@pytest.mark.parametrize(
    "overrides,exception,message",
    [
        pytest.param(
            {"actor_id": "foreign-operator"},
            AssignmentDenied,
            "assignment actor does not match trusted principal",
            id="foreign-actor",
        ),
        pytest.param(
            {"actor_type": "user"},
            AssignmentDenied,
            "assignment actor does not match trusted principal",
            id="foreign-actor-type",
        ),
        pytest.param(
            {"workspace_id": uuid4()},
            NotFound,
            "planning run not found",
            id="foreign-workspace",
        ),
        pytest.param(
            {"target_type": "task"},
            ValidationError,
            "requires a planning_run target",
            id="wrong-target-type",
        ),
        pytest.param(
            {"target_id": None},
            ValidationError,
            "requires a planning_run target",
            id="missing-target",
        ),
        pytest.param(
            {"target_id": uuid4()},
            NotFound,
            "planning run not found",
            id="stale-target",
        ),
        pytest.param(
            {"expected_version": 1},
            VersionConflict,
            "version",
            id="stale-version",
        ),
        pytest.param(
            {"expected_version": None},
            ValidationError,
            "requires target_id and expected_version",
            id="missing-version",
        ),
        pytest.param(
            {"payload": {"unknown_outcome_decision": "inspect"}},
            ValidationError,
            "unknown_outcome_decision must be retry",
            id="wrong-decision",
        ),
    ],
)
def test_native_prestart_context_retry_keeps_command_scope_and_version_guards(
    overrides: dict[str, Any], exception: type[Exception], message: str
) -> None:
    case = native_prestart_context_retry_case()
    case.command = replace(case.command, **overrides)
    assert_native_prestart_context_retry_refused(case, exception, message)


def test_native_prestart_context_retry_keeps_authorization_denial() -> None:
    case = native_prestart_context_retry_case(deny_grant=True)
    assert_native_prestart_context_retry_refused(
        case, AssignmentDenied, "native planning scope denied"
    )
    assert len(case.authorization_targets) == 1
    assert case.resolved_profiles == []


def test_native_prestart_context_retry_keeps_current_run_guard() -> None:
    case = native_prestart_context_retry_case()
    case.request.retry_blocked(new_run_id=uuid4(), now=NOW)
    assert_native_prestart_context_retry_refused(
        case, ValidationError, "blocked run is not the current planning run"
    )


def test_native_prestart_context_retry_keeps_saved_run_policy_guard() -> None:
    case = native_prestart_context_retry_case()
    case.run.profile_snapshot = replace(
        case.run.profile_snapshot,
        planner_agent_ref=ProfileRef(
            workspace_id=WORKSPACE_ID, profile_id=uuid4(), revision=1
        ),
    )
    assert_native_prestart_context_retry_refused(
        case, AssignmentDenied, "planning Run policy differs from its saved request"
    )


@pytest.mark.parametrize("link_status", ["closed", "rejected", "waiting_capacity"])
@pytest.mark.parametrize(
    "error_code",
    [
        "timeout",
        "rate_limited",
        "invalid_planner_output",
        "invalid_result",
        "proposal_schema_invalid",
    ],
)
def test_native_prestart_context_retry_preserves_classified_closed_boundary_retry(
    link_status: str, error_code: str
) -> None:
    case = native_prestart_context_retry_case(
        started=True,
        error_code=error_code,
        max_retries=1,
        backoff_seconds=(5,),
        invalid_output_policy=InvalidOutputPolicy.RETRY,
    )
    old_run = replace(case.run)
    case.unit.planning_authorities.link = native_retry_link(case, link_status)
    case.unit.context_assemblies.stored = None
    result = case.handler.handle(case.unit, case.command)
    new_run = case.request.runs[-1]
    assert result.data == {
        "planning_request_id": str(case.request.id),
        "planning_run_id": str(new_run.id),
        "attempt": 2,
        "unknown_outcome_decision": "retry",
    }
    assert len(case.request.runs) == 2
    assert case.run == old_run
    assert case.unit.planning_runs.added == [new_run]
    assert case.unit.planning_runs.saved == []
    assert len(case.unit.planning_requests.saved) == 1
    assert len(case.unit.context_assemblies.added) == 1
    assert case.unit.context_assemblies.added[0].consumer_id == new_run.id
    assert case.unit.context_assemblies.reads == []
    assert len(case.unit.events.events) == 1


@pytest.mark.parametrize("link_status", ["closed", "rejected", "waiting_capacity"])
@pytest.mark.parametrize(
    "policy_values,message",
    [
        pytest.param(
            {"max_retries": 0, "backoff_seconds": ()},
            "native planning policy does not permit this classified retry",
            id="cap0-empty-backoff",
        ),
        pytest.param(
            {"retries_used": 1},
            "native planning policy does not permit this classified retry",
            id="exhausted",
        ),
        pytest.param(
            {"retry_delay_seconds": 4},
            "native planning retry backoff has not elapsed",
            id="backoff",
        ),
        pytest.param(
            {
                "error_code": "invalid_planner_output",
                "invalid_output_policy": InvalidOutputPolicy.BLOCK,
            },
            "native planning policy does not permit this classified retry",
            id="invalid-output-block",
        ),
        *[
            pytest.param(
                {"error_code": error_code},
                "native planning policy does not permit this classified retry",
                id=error_code,
            )
            for error_code in (
                "temporary_unavailable",
                "native_admission_unknown",
                "provider_unknown_outcome",
            )
        ],
    ],
)
def test_native_prestart_context_retry_keeps_admitted_policy_refusals(
    link_status: str, policy_values: dict[str, Any], message: str
) -> None:
    values = {
        "started": True,
        "error_code": "timeout",
        "max_retries": 1,
        "backoff_seconds": (5,),
        "invalid_output_policy": InvalidOutputPolicy.RETRY,
    }
    values.update(policy_values)
    case = native_prestart_context_retry_case(**values)
    case.unit.planning_authorities.link = native_retry_link(case, link_status)
    assert_native_prestart_context_retry_refused(case, ValidationError, message)
    assert case.unit.context_assemblies.reads == []


def test_planning_handler_registers_submit_plan_revision() -> None:
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    assert "SubmitPlanRevision" in handler.command_types


def test_submit_plan_revision_payload_uses_strict_command_schema() -> None:
    root_task_id = uuid4()

    with pytest.raises(ValueError, match="extra"):
        CommandRequest(
            command_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            command_type="SubmitPlanRevision",
            target_type="task",
            actor=ActorRequest(type="user", id="operator"),
            target_id=root_task_id,
            expected_version=1,
            idempotency_key=uuid4().hex,
            correlation_id=uuid4(),
            payload={
                "root_task_id": str(root_task_id),
                "expected_base_snapshot_id": str(uuid4()),
                "expected_base_generation": 4,
                "expected_base_content_hash": PROPOSAL_HASH,
                "objective": "Revise the running plan",
                "constraints": [],
                "acceptance_criteria": ["The revision is reviewable"],
                "planning_profile_id": str(uuid4()),
                "planning_profile_version": 1,
                "unexpected": True,
            },
        )


def _revision_fixture(
    *,
    proposal_schema_versions: tuple[int, ...] = (2, 3),
    proposal_schema_version: int = 3,
) -> tuple[Any, Task, TaskPlanSnapshot, PlanningProfile]:
    definition, instance = module_facts(
        proposal_schema_versions=proposal_schema_versions
    )
    stored_profile = profile(
        instance.id, proposal_schema_version=proposal_schema_version
    )
    profile_snapshot = stored_profile.snapshot(
        planner_capabilities(proposal_schema_versions)
    )
    stored_request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_snapshot=profile_snapshot,
        objective="Create the original plan",
        constraints=(),
        acceptance_criteria=("The original plan is reviewable",),
        context_refs=(),
        requested_outputs=("plan",),
        source_type="web",
        source_ref=None,
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )
    root_task = Task.create(
        task_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        title="Original root task",
        description="Root task under revision test",
        acceptance_criteria=("The task remains unchanged",),
        priority=50,
        requested_by="operator",
    )
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=uuid4(),
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=root_task.id,
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="revision-test",
        title="Original plan",
        goal_summary="Original plan for revision command",
        acceptance_refs=("plan:reviewable",),
        published_by="operator",
        published_at=NOW,
    )
    head = RootTaskPlanHead(
        workspace_id=WORKSPACE_ID,
        root_task_id=root_task.id,
        active_snapshot_id=snapshot.id,
        active_generation=snapshot.generation,
        active_snapshot_hash=plan_snapshot_content_hash(snapshot),
        version=1,
        updated_at=NOW,
    )
    unit_of_work = unit(definition, instance, stored_profile, stored_request)
    unit_of_work.tasks = TaskRepository(root_task)
    unit_of_work.plans = PlanRepository(snapshot, head)
    assert unit_of_work.work_contexts.context is not None
    unit_of_work.work_contexts.context.root_task_id = root_task.id
    return unit_of_work, root_task, snapshot, stored_profile


def _submit_revision_envelope(
    root_task: Task,
    snapshot: TaskPlanSnapshot,
    profile_item: PlanningProfile,
    *,
    expected_task_version: int | None = None,
    expected_generation: int | None = None,
    expected_content_hash: str | None = None,
) -> CommandEnvelope:
    return envelope(
        "SubmitPlanRevision",
        {
            "root_task_id": str(root_task.id),
            "expected_base_snapshot_id": str(snapshot.id),
            "expected_base_generation": (
                snapshot.generation
                if expected_generation is None
                else expected_generation
            ),
            "expected_base_content_hash": (
                plan_snapshot_content_hash(snapshot)
                if expected_content_hash is None
                else expected_content_hash
            ),
            "objective": "Revise the running plan",
            "constraints": ["Keep completed work reusable"],
            "acceptance_criteria": ["The revision is reviewable"],
            "planning_profile_id": str(profile_item.id),
            "planning_profile_version": profile_item.version,
        },
        target_type="task",
        target_id=root_task.id,
        expected_version=(
            root_task.version
            if expected_task_version is None
            else expected_task_version
        ),
    )


def test_submit_plan_revision_creates_queued_run_and_reuses_context_lineage() -> None:
    unit_of_work, root_task, snapshot, stored_profile = _revision_fixture()
    old_head = (
        unit_of_work.plans.head.active_snapshot_id,
        unit_of_work.plans.head.active_generation,
        unit_of_work.plans.head.active_snapshot_hash,
        unit_of_work.plans.head.version,
    )
    old_root = (root_task.status, root_task.version, root_task.title)
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    result = handler.handle(
        unit_of_work,
        _submit_revision_envelope(root_task, snapshot, stored_profile),
    )

    revision_request = unit_of_work.planning_requests.added[-1]
    assert revision_request.kind.value == "revision"
    assert revision_request.root_task_id == root_task.id
    assert revision_request.base_descriptor is not None
    assert revision_request.base_descriptor.plan_snapshot_id == snapshot.id
    assert revision_request.base_descriptor.plan_generation == snapshot.generation
    assert revision_request.base_descriptor.plan_content_hash == old_head[2]
    assert revision_request.runs[-1].status is PlanningRunStatus.QUEUED
    assert result.data["planning_request_id"] == str(revision_request.id)
    assert result.data["planning_run_id"] == str(revision_request.current_run_id)
    assert result.data["base_snapshot_id"] == str(snapshot.id)
    assert result.data["base_generation"] == 1
    assert unit_of_work.work_contexts.context is not None
    assert unit_of_work.work_contexts.context.root_task_id == root_task.id
    assert unit_of_work.work_contexts.context.current_revision_number == 2
    revision = unit_of_work.work_contexts.appended[-1][0]
    assert revision.reason is WorkContextRevisionReason.REPLANNED
    assert revision.brief_planning_request_id == revision_request.id
    assert (
        unit_of_work.context_assemblies.added[-1].consumer_id
        == revision_request.current_run_id
    )
    assert [event.event_type for event in unit_of_work.events.events[-2:]] == [
        "WorkContextRevised",
        "PlanRevisionRequested",
    ]
    assert (
        unit_of_work.plans.head.active_snapshot_id,
        unit_of_work.plans.head.active_generation,
        unit_of_work.plans.head.active_snapshot_hash,
        unit_of_work.plans.head.version,
    ) == old_head
    assert (root_task.status, root_task.version, root_task.title) == old_root


def test_submit_plan_revision_rejects_stale_generation_without_writes() -> None:
    unit_of_work, root_task, snapshot, stored_profile = _revision_fixture()
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    with pytest.raises(PlanGenerationConflict):
        handler.handle(
            unit_of_work,
            _submit_revision_envelope(
                root_task,
                snapshot,
                stored_profile,
                expected_generation=2,
            ),
        )

    assert unit_of_work.planning_requests.added == []
    assert unit_of_work.work_contexts.appended == []
    assert unit_of_work.context_assemblies.added == []
    assert unit_of_work.events.events == []


def test_submit_plan_revision_rejects_stale_root_task_without_writes() -> None:
    unit_of_work, root_task, snapshot, stored_profile = _revision_fixture()
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )
    root_task.version = 2

    with pytest.raises(VersionConflict):
        handler.handle(
            unit_of_work,
            _submit_revision_envelope(
                root_task,
                snapshot,
                stored_profile,
                expected_task_version=1,
            ),
        )

    assert unit_of_work.planning_requests.added == []
    assert unit_of_work.work_contexts.appended == []
    assert unit_of_work.context_assemblies.added == []
    assert unit_of_work.events.events == []


def test_submit_plan_revision_rejects_v2_profile_and_existing_active_revision() -> None:
    unit_of_work, root_task, snapshot, v2_profile = _revision_fixture(
        proposal_schema_version=2
    )
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    with pytest.raises(ValidationError, match="schema v3"):
        handler.handle(
            unit_of_work,
            _submit_revision_envelope(root_task, snapshot, v2_profile),
        )

    active_revision = SimpleNamespace(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=root_task.id,
        version=4,
        current_proposal_revision=None,
        current_proposal_content_hash=None,
    )
    unit_of_work.planning_requests.active_revision = active_revision
    v3_profile = profile(unit_of_work.modules.instance.id)
    unit_of_work.planning_profiles.stored = v3_profile
    with pytest.raises(PlanningConflict):
        handler.handle(
            unit_of_work,
            _submit_revision_envelope(root_task, snapshot, v3_profile),
        )


def test_submit_plan_revision_accepts_v4_profile() -> None:
    unit_of_work, root_task, snapshot, v4_profile = _revision_fixture(
        proposal_schema_versions=(3, 4),
        proposal_schema_version=4,
    )
    handler = PlanningCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    result = handler.handle(
        unit_of_work,
        _submit_revision_envelope(root_task, snapshot, v4_profile),
    )

    assert result.data["planning_request_id"]
    assert result.data["planning_run_id"]


class RepairLeaseRepository(LeaseRepository):
    def __init__(self, run_id: UUID) -> None:
        super().__init__()
        self.run_id = run_id
        self.active: PlanningRunLease | None = None
        self.reads: list[tuple[UUID, UUID, bool]] = []

    def get_active_for_run(self, run_id, workspace_id, *, for_update=False):
        assert (run_id, workspace_id, for_update) == (self.run_id, WORKSPACE_ID, True)
        self.reads.append((run_id, workspace_id, for_update))
        return self.active


def _failed_context_repair_case(
    *, revision=False, expired=False, deny_grant=False, planner_revision=3
):
    case = _proposal_context_case(revision=revision, native=not revision)
    request, uow = case.request, case.unit
    policy = replace(
        uow.planning_profiles.stored,
        execution_mode="native_profile",
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_agent_ref=ProfileRef(
            workspace_id=WORKSPACE_ID, profile_id=uuid4(), revision=planner_revision
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=0,
            backoff_seconds=(),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.BLOCK,
        ),
    )
    request.profile_snapshot = policy.snapshot()
    request.runs = tuple(
        replace(
            run,
            profile_snapshot=request.profile_snapshot,
            planner_module_instance_id=None,
            planner_module_config_version=None,
            capability_snapshot=request.profile_snapshot.planner_capabilities,
        )
        for run in request.runs
    )
    uow.planning_profiles.stored = policy
    if not revision:
        root = Task.create(
            task_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            title="Retained Task",
            description="Original submitted goal",
            acceptance_criteria=("The Task is preserved",),
            priority=50,
            requested_by="operator",
        )
        uow.tasks = TaskRepository(root)
    context = uow.work_contexts.context
    old = uow.work_contexts.revisions[context.current_revision_id]
    snapshot = replace(
        old.snapshot,
        objective="Retained context before feedback",
        clarification_decisions=(
            ClarificationDecision(
                "adapter", "Which adapter?", "opencode", "operator", 0
            ),
        ),
        accepted_decisions=(
            AcceptedDecision("review", "Keep review gates", "user", 0),
        ),
        unresolved_questions=(
            UnresolvedQuestion("owner", "Who owns delivery?", True, 0),
        ),
        knowledge_refs=(
            KnowledgeRevisionRef(
                uuid4(), uuid4(), "b" * 64, "overview", "c" * 64, (uuid4(),), 0
            ),
        ),
        artifact_refs=(WorkReference(uuid4(), "artifact", 0),),
        evidence_refs=(WorkReference(uuid4(), "evidence", 0),),
        checkpoint_refs=(WorkReference(uuid4(), "checkpoint", 0),),
        missing_fact_codes=("missing-operator-fact",),
    )
    old = replace(
        old,
        snapshot=snapshot,
        content_hash=snapshot.content_hash,
        plan_snapshot_id=(
            request.base_descriptor.plan_snapshot_id if revision else uuid4()
        ),
    )
    uow.work_contexts.revisions[old.revision_id] = old
    brief = PlanningBriefRevision.create(
        planning_request_id=request.id,
        revision=2,
        objective=request.briefs[-1].objective,
        constraints=request.briefs[-1].constraints,
        acceptance_criteria=request.briefs[-1].acceptance_criteria,
        context_refs=request.briefs[-1].context_refs,
        requested_outputs=request.briefs[-1].requested_outputs,
        user_feedback="Split implementation and verification.",
        created_by="operator",
        source_correlation=uuid4(),
        created_at=NOW,
    )
    # Construct the persisted legacy mismatch without undoing the accepted producer.
    request.request_revision(
        expected_proposal_revision=1,
        expected_content_hash=PROPOSAL_HASH,
        feedback=brief.user_feedback,
        brief=brief,
        new_run_id=uuid4(),
        actor="operator",
        now=NOW,
    )
    run = request.runs[-1]
    lease_id = uuid4()
    request.start_planning(run_id=run.id, lease_id=lease_id, fencing_token=1, now=NOW)
    request.fail_run(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        error_code="permission_denied",
        error_message="Task source access denied.",
        diagnostic_artifact_ref="retained-negative-receipt",
        now=NOW,
    )
    run.provider_ref, run.structured_result_type = "agent-run", "failure"
    run.usage = PlanningUsage.unknown()
    run.lease_id, run.fencing_token = None, 0
    uow.planning_leases = RepairLeaseRepository(run.id)
    uow.planning_records.briefs = list(request.briefs)
    assembly = build_planning_context_assembly_request(
        run=run,
        brief=brief,
        work_context_revision=old,
        ttl_seconds=1 if expired else 1800,
        created_at=NOW,
        planning_kind=request.kind,
        revision_base=request.base_descriptor,
    )
    uow.context_assemblies.stored = replace(
        assembly, status=ContextAssemblyStatus.READY
    )
    uow.context_assemblies.expected_consumer = (
        ContextPurpose.PLANNING_RUN,
        run.id,
        WORKSPACE_ID,
    )
    identity = TrustedAssignmentIdentity("operator", uuid4(), actor_type="user")
    goal = (
        None
        if revision
        else TaskGoalSubmission(
            WORKSPACE_ID,
            uow.tasks.task.id,
            request.id,
            uow.tasks.task.version,
            identity.principal_id,
            identity.grant_id,
        )
    )
    uow.task_goals = SimpleNamespace(
        get=lambda workspace, request_id: goal, submission=goal
    )
    grant_reads, profile_reads, link_reads = [], [], []

    def require(grant_id, action, target):
        assert grant_id == identity.grant_id
        assert action == "planning.write"
        assert target == PlanningCommandTarget(
            WORKSPACE_ID,
            "operator",
            request.id,
            policy.id,
            policy.version,
            policy.planner_agent_ref,
            None,
            request.briefs[-1].context_refs,
            request.root_task_id if revision else goal.root_task_id,
        )
        grant_reads.append(target)
        if deny_grant:
            raise AgentError("permission_denied", "Denied grant")

    def resolve_profile(workspace, profile_id, profile_revision):
        assert (workspace, profile_id, profile_revision) == (
            WORKSPACE_ID,
            policy.planner_agent_ref.profile_id,
            policy.planner_agent_ref.revision,
        )
        profile_reads.append(policy.planner_agent_ref)
        return policy.planner_agent_ref

    def get_link(workspace, run_id, *, for_update=False):
        assert (workspace, run_id, for_update) == (WORKSPACE_ID, run.id, True)
        link_reads.append((workspace, run_id, for_update))
        return uow.planning_authorities.link

    uow.planning_authorities = SimpleNamespace(get_link=get_link, link=None)
    repair_at = NOW + timedelta(seconds=60)
    case.handler = PlanningCommandHandler(
        local_actor_type="user",
        local_actor_id="operator",
        clock=lambda: repair_at,
        identity=identity,
        authorizer=SimpleNamespace(require=require),
        assignment_handler=SimpleNamespace(resolve_profile=resolve_profile),
        planning_context_ttl_seconds=900,
    )
    case.command = envelope(
        "RepairPlanningContext",
        {
            "expected_planning_request_version": request.version,
            "expected_work_context_version": context.version,
            "work_context_revision_id": str(old.revision_id),
            "work_context_content_hash": old.content_hash,
        },
        actor_type="user",
        target_type="planning_run",
        target_id=run.id,
        expected_version=run.version,
    )
    case.run, case.profile, case.revision = run, policy, old
    case.goal, case.identity, case.repair_at = goal, identity, repair_at
    case.grant_reads, case.profile_reads, case.link_reads = (
        grant_reads,
        profile_reads,
        link_reads,
    )
    uow.planning_requests.added.clear()
    uow.planning_requests.saved.clear()
    uow.planning_runs.added.clear()
    uow.work_contexts.appended.clear()
    uow.context_assemblies.added.clear()
    uow.events.events.clear()
    return case


def _repair_state(case):
    uow = case.unit
    context = uow.work_contexts.context
    return (
        {
            **{
                name: getattr(case.request, name)
                for name in case.request.__dataclass_fields__
            },
            "runs": tuple(
                {name: getattr(run, name) for name in run.__dataclass_fields__}
                for run in case.request.runs
            ),
            "clarifications": tuple(
                replace(item) for item in case.request.clarifications
            ),
        },
        None if context is None else replace(context),
        dict(uow.work_contexts.revisions),
        uow.context_assemblies.stored,
        uow.planning_authorities.link,
        None
        if uow.planning_leases.active is None
        else replace(uow.planning_leases.active),
        tuple(uow.planning_leases.saved),
        tuple(uow.planning_requests.added),
        tuple(uow.planning_requests.saved),
        tuple(uow.planning_runs.added),
        tuple(uow.planning_runs.saved),
        tuple(uow.work_contexts.added),
        tuple(uow.work_contexts.appended),
        tuple(uow.context_assemblies.added),
        tuple(uow.planning_records.briefs),
        tuple(uow.planning_records.clarifications),
        tuple(uow.planning_records.saved_clarifications),
        dict(vars(case.proposal)),
        tuple(uow.events.events),
        tuple(uow.projection_changes.changes),
        uow.task_goals.submission,
        replace(uow.tasks.task),
        (replace(uow.plans.head), uow.plans.snapshot)
        if case.request.kind is PlanningRequestKind.REVISION
        else None,
    )


@pytest.mark.parametrize("revision", [False, True], ids=["create", "revision"])
@pytest.mark.parametrize("expired", [False, True], ids=["fresh", "expired"])
def test_repair_planning_context_preserves_failed_history_and_retained_lineage(
    revision, expired
):
    case = _failed_context_repair_case(revision=revision, expired=expired)
    uow, request = case.unit, case.request
    before = _repair_state(case)
    old_context = replace(uow.work_contexts.context)
    original_runs = tuple(replace(run) for run in request.runs)
    briefs = request.briefs
    original_assembly = uow.context_assemblies.stored
    old_request_version = request.version

    result = case.handler.handle(uow, case.command)

    context, successor = uow.work_contexts.context, request.runs[-1]
    current = uow.work_contexts.revisions[context.current_revision_id]
    assert request.id == before[0]["id"]
    assert request.status is PlanningRequestStatus.PLANNING
    assert request.version == old_request_version + 1
    assert request.briefs == briefs and request.current_brief_revision == 2
    assert request.runs[:-1] == original_runs
    assert request.current_run_id == successor.id != case.run.id
    assert successor.operation_id != case.run.operation_id
    assert successor.status is PlanningRunStatus.QUEUED
    assert successor.attempt == case.run.attempt + 1 and successor.brief_revision == 2
    assert successor.profile_snapshot == request.profile_snapshot
    assert request.profile_snapshot.retry_policy.max_retries == 0
    assert (context.id, context.planning_request_id, context.root_task_id) == (
        old_context.id,
        old_context.planning_request_id,
        old_context.root_task_id,
    )
    assert context.version == old_context.version + 1
    assert current.revision_number == old_context.current_revision_number + 1
    assert (
        current.brief_planning_request_id == request.id and current.brief_revision == 2
    )
    assert current.plan_snapshot_id == case.revision.plan_snapshot_id
    assert current.reason is (
        WorkContextRevisionReason.REPLANNED
        if revision
        else WorkContextRevisionReason.USER_INTERVENTION
    )
    assert current.snapshot.objective == briefs[-1].objective
    for field in (
        "clarification_decisions",
        "accepted_decisions",
        "unresolved_questions",
        "knowledge_refs",
        "artifact_refs",
        "evidence_refs",
        "checkpoint_refs",
        "missing_fact_codes",
    ):
        assert getattr(current.snapshot, field) == getattr(
            case.revision.snapshot, field
        )
    assert uow.work_contexts.revisions[case.revision.revision_id] is case.revision
    assert uow.work_contexts.appended == [(current, old_context.version)]
    assert uow.planning_runs.added == [successor] and uow.planning_runs.saved == []
    assert uow.planning_requests.saved == [(request, old_request_version)]
    assert uow.planning_records.briefs == list(briefs)
    assert dict(vars(case.proposal)) == before[17]
    assert replace(uow.tasks.task) == before[21]
    assert uow.task_goals.submission == before[20]
    if revision:
        assert (uow.plans.head, uow.plans.snapshot) == before[22]
        for field in (
            "base_plan_snapshot_id",
            "base_plan_generation",
            "base_plan_content_hash",
            "base_root_task_version",
            "base_captured_at",
        ):
            assert getattr(request, field) == before[0][field]
        assert context.planning_request_id != request.id
    assert uow.context_assemblies.stored is original_assembly
    assert len(uow.context_assemblies.added) == 1
    assembly = uow.context_assemblies.added[0]
    assert (
        assembly.consumer_id == successor.id
        and assembly.work_context_revision_id == current.revision_id
    )
    assert assembly.status is ContextAssemblyStatus.QUEUED
    assert assembly.expires_at == case.repair_at + timedelta(seconds=900)
    assert assembly.idempotency_key != original_assembly.idempotency_key
    assert [event.event_type for event in uow.events.events] == [
        "WorkContextRevised",
        "PlanningContextRepaired",
    ]
    assert result.event_ids == tuple(event.id for event in uow.events.events)
    assert (
        result.aggregate_id == request.id
        and result.aggregate_version == request.version
    )
    assert result.data == {
        "old_planning_run_id": str(case.run.id),
        "planning_run_id": str(successor.id),
        "planning_request_id": str(request.id),
        "attempt": successor.attempt,
        "brief_revision": 2,
        "work_context_id": str(context.id),
        "work_context_revision": current.revision_number,
        "work_context_content_hash": current.content_hash,
    }
    assert uow.events.events[-1].payload == result.data
    assert case.grant_reads and case.profile_reads
    assert uow.planning_leases.reads == [(case.run.id, WORKSPACE_ID, True)]
    assert case.link_reads == [(WORKSPACE_ID, case.run.id, True)]
    assert (
        "root" if revision else "request",
        context.root_task_id if revision else request.id,
        WORKSPACE_ID,
        True,
    ) in uow.work_contexts.lookups


_REPAIR_REFUSALS = (
    "run_status",
    "request_status",
    "current_run",
    "latest_run",
    "run_version",
    "request_version",
    "context_version",
    "context_id",
    "context_hash",
    "missing_context",
    "archived_context",
    "legacy_context",
    "foreign_context_workspace",
    "foreign_context_owner",
    "foreign_context_root",
    "missing_revision",
    "untyped_revision",
    "revision_workspace",
    "revision_context",
    "revision_number",
    "revision_owner",
    "aligned_context",
    "brief_gap",
    "missing_brief",
    "brief_owner",
    "current_brief",
    "run_brief",
    "failure_code",
    "result_kind",
    "provider_ref",
    "missing_start",
    "missing_finish",
    "active_lease",
    "missing_assembly",
    "untyped_assembly",
    "assembly_status",
    "assembly_workspace",
    "assembly_consumer",
    "assembly_purpose",
    "assembly_type",
    "assembly_revision",
    "assembly_owner",
    "assembly_expiry",
    "assembly_profile",
    "assembly_profile_version",
    "assembly_mode",
    "assembly_agent",
    "assembly_kind",
    "nonnative",
    "saved_policy",
    "locked_policy",
    "foreign_run_workspace",
    "foreign_request_workspace",
    "foreign_profile_workspace",
)


def _damage_repair_case(case, damage):
    request, run, uow = case.request, case.run, case.unit
    context = uow.work_contexts.context
    old, assembly = case.revision, uow.context_assemblies.stored
    if damage == "run_status":
        run.status = PlanningRunStatus.BLOCKED
    elif damage == "request_status":
        request.status = PlanningRequestStatus.PROPOSAL_READY
    elif damage == "current_run":
        request.runs = (*request.runs, request.runs[0])
    elif damage == "latest_run":
        request.runs = (*request.runs, replace(run, id=uuid4()))
    elif damage in {
        "run_version",
        "request_version",
        "context_version",
        "context_id",
        "context_hash",
    }:
        key, value = {
            "run_version": (None, run.version + 1),
            "request_version": (
                "expected_planning_request_version",
                request.version + 1,
            ),
            "context_version": ("expected_work_context_version", context.version + 1),
            "context_id": ("work_context_revision_id", str(uuid4())),
            "context_hash": ("work_context_content_hash", "f" * 64),
        }[damage]
        case.command = (
            replace(case.command, expected_version=value)
            if key is None
            else replace(case.command, payload={**case.command.payload, key: value})
        )
    elif damage == "missing_context":
        uow.work_contexts.context = None
    elif damage in {"archived_context", "legacy_context"}:
        context.status = (
            WorkContextStatus.ARCHIVED
            if damage == "archived_context"
            else WorkContextStatus.LEGACY_INCOMPLETE
        )
    elif damage.startswith("foreign_context"):
        setattr(
            context,
            {
                "foreign_context_workspace": "workspace_id",
                "foreign_context_owner": "planning_request_id",
                "foreign_context_root": "root_task_id",
            }[damage],
            uuid4(),
        )
        uow.work_contexts.get_by_planning_request = lambda *args, **kwargs: context
    elif damage == "missing_revision":
        uow.work_contexts.revisions.clear()
    elif damage == "untyped_revision":
        uow.work_contexts.get_revision = lambda *args, **kwargs: SimpleNamespace(
            **{name: getattr(old, name) for name in old.__dataclass_fields__}
        )
    elif damage == "brief_gap":
        request.current_brief_revision = run.brief_revision = 3
        request.briefs = (*request.briefs[:-1], replace(request.briefs[-1], revision=3))
    elif damage.startswith("revision_") or damage == "aligned_context":
        field, value = {
            "revision_workspace": ("workspace_id", uuid4()),
            "revision_context": ("work_context_id", uuid4()),
            "revision_number": ("revision_number", old.revision_number + 1),
            "revision_owner": ("brief_planning_request_id", uuid4()),
            "aligned_context": ("brief_revision", 2),
        }[damage]
        changed = replace(old, **{field: value})
        uow.work_contexts.get_revision = lambda *args, **kwargs: changed
    elif damage == "missing_brief":
        request.briefs = ()
    elif damage == "brief_owner":
        request.briefs = (
            *request.briefs[:-1],
            replace(request.briefs[-1], planning_request_id=uuid4()),
        )
    elif damage == "current_brief":
        request.current_brief_revision = 1
    elif damage == "run_brief":
        run.brief_revision = 1
    elif damage in {
        "failure_code",
        "result_kind",
        "provider_ref",
        "missing_start",
        "missing_finish",
    }:
        field, value = {
            "failure_code": ("error_code", "timeout"),
            "result_kind": ("structured_result_type", "proposal"),
            "provider_ref": ("provider_ref", "actual-provider-receipt"),
            "missing_start": ("started_at", None),
            "missing_finish": ("finished_at", None),
        }[damage]
        setattr(run, field, value)
    elif damage == "active_lease":
        assert run.lease_id is None and run.fencing_token == 0
        uow.planning_leases.active = PlanningRunLease(
            uuid4(),
            WORKSPACE_ID,
            run.id,
            "retained-owner",
            PlanningRunLeaseStatus.ACTIVE,
            9,
            NOW,
            NOW + timedelta(seconds=300),
            None,
            1,
        )
    elif damage == "missing_assembly":
        uow.context_assemblies.stored = None
    elif damage == "untyped_assembly":
        uow.context_assemblies.stored = SimpleNamespace(
            status=ContextAssemblyStatus.READY
        )
    elif damage.startswith("assembly_"):
        field, value = {
            "assembly_status": ("status", ContextAssemblyStatus.BLOCKED),
            "assembly_workspace": ("workspace_id", uuid4()),
            "assembly_consumer": ("consumer_id", uuid4()),
            "assembly_purpose": ("purpose", ContextPurpose.EXECUTION),
            "assembly_type": ("consumer_type", ContextConsumerType.WORKER),
            "assembly_revision": ("work_context_revision_id", uuid4()),
            "assembly_owner": ("lease_owner", "retained-owner"),
            "assembly_expiry": ("lease_expires_at", NOW + timedelta(seconds=300)),
        }.get(damage, (None, None))
        if field is None:
            key, value = {
                "assembly_profile": ("profile_id", str(uuid4())),
                "assembly_profile_version": ("profile_version", 2),
                "assembly_mode": ("execution_mode", "legacy_module"),
                "assembly_agent": ("planner_agent_ref", {"unexpected": "pin"}),
                "assembly_kind": ("planning_kind", "revision"),
            }[damage]
            field, value = "budget", {**assembly.budget, key: value}
        if damage in {"assembly_purpose", "assembly_type"}:
            object.__setattr__(assembly, field, value)
        else:
            uow.context_assemblies.stored = replace(assembly, **{field: value})
    elif damage == "nonnative":
        legacy = profile(uow.modules.instance.id).snapshot(planner_capabilities())
        run.profile_snapshot = request.profile_snapshot = legacy
    elif damage == "saved_policy":
        run.profile_snapshot = replace(
            run.profile_snapshot, prompt_policy_version="other-policy"
        )
    elif damage == "locked_policy":
        locked_run = replace(
            run,
            profile_snapshot=replace(
                run.profile_snapshot, prompt_policy_version="other-policy"
            ),
        )
        uow.planning_runs.get = lambda *args, for_update=False: (
            locked_run if for_update else run
        )
    elif damage == "foreign_run_workspace":
        run.workspace_id = uuid4()
        uow.planning_runs.get = lambda *args, **kwargs: run
    elif damage == "foreign_request_workspace":
        request.workspace_id = uuid4()
        uow.planning_requests.get = lambda *args, **kwargs: request
    elif damage == "foreign_profile_workspace":
        foreign = uuid4()
        run.profile_snapshot = request.profile_snapshot = replace(
            request.profile_snapshot,
            workspace_id=foreign,
            planner_agent_ref=request.profile_snapshot.planner_agent_ref.model_copy(
                update={"workspace_id": foreign}
            ),
        )
    else:
        raise AssertionError(damage)


@pytest.mark.parametrize("damage", _REPAIR_REFUSALS)
def test_repair_planning_context_refuses_unproven_state_without_any_delta(damage):
    case = _failed_context_repair_case()
    _damage_repair_case(case, damage)
    before = _repair_state(case)
    with pytest.raises(
        (ValidationError, VersionConflict, AssignmentDenied, NotFound)
    ) as denied:
        case.handler.handle(case.unit, case.command)
    assert "unsupported" not in str(denied.value)
    assert _repair_state(case) == before


@pytest.mark.parametrize(
    "field",
    [
        "input_tokens",
        "output_tokens",
        "cost_microunits",
        "runtime_seconds",
        "context_bytes",
    ],
)
def test_repair_planning_context_refuses_even_zero_observed_usage(field):
    case = _failed_context_repair_case()
    case.run.usage = replace(PlanningUsage.unknown(), **{field: 0})
    before = _repair_state(case)
    with pytest.raises(ValidationError, match="planning context repair"):
        case.handler.handle(case.unit, case.command)
    assert _repair_state(case) == before


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "starting",
        "rejected",
        "waiting_capacity",
        "unknown",
        "running",
        "cleanup_pending",
        "closed",
    ],
)
def test_repair_planning_context_refuses_any_native_link_including_closed(status):
    case = _failed_context_repair_case()
    case.unit.planning_authorities.link = native_retry_link(case, status)
    before = _repair_state(case)
    with pytest.raises(ValidationError, match="native planning link"):
        case.handler.handle(case.unit, case.command)
    assert _repair_state(case) == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing_root_context",
        "request_owned_context",
        "foreign_root",
        "foreign_base_workspace",
        "wrong_budget_base",
        "missing_budget_base",
    ],
)
def test_repair_planning_context_revision_refuses_foreign_or_fallback_lineage(damage):
    case = _failed_context_repair_case(revision=True)
    request, uow = case.request, case.unit
    context = uow.work_contexts.context
    if damage == "missing_root_context":
        uow.work_contexts.get_by_root_task = lambda *args, **kwargs: None
        uow.work_contexts.get_by_planning_request = lambda *args, **kwargs: context
    elif damage == "request_owned_context":
        context.planning_request_id = request.id
    elif damage == "foreign_root":
        context.root_task_id = uuid4()
        uow.work_contexts.get_by_root_task = lambda *args, **kwargs: context
    elif damage == "foreign_base_workspace":
        request.workspace_id = uuid4()
        uow.planning_requests.get = lambda *args, **kwargs: request
    else:
        assembly = uow.context_assemblies.stored
        budget = dict(assembly.budget)
        if damage == "missing_budget_base":
            del budget["revision_base"]
        else:
            budget["revision_base"] = {
                **budget["revision_base"],
                "root_task_id": str(uuid4()),
            }
        uow.context_assemblies.stored = replace(assembly, budget=budget)
    before = _repair_state(case)
    with pytest.raises((ValidationError, AssignmentDenied, NotFound)) as denied:
        case.handler.handle(uow, case.command)
    assert "unsupported" not in str(denied.value)
    assert _repair_state(case) == before


@pytest.mark.parametrize("field", ["schema_version", "generation", "root_task_version"])
def test_repair_planning_context_requires_type_exact_revision_base_pins(field):
    case = _failed_context_repair_case(revision=True)
    assembly = case.unit.context_assemblies.stored
    base = dict(assembly.budget["revision_base"])
    assert base[field] == 1
    base[field] = True
    case.unit.context_assemblies.stored = replace(
        assembly, budget={**assembly.budget, "revision_base": base}
    )
    before = _repair_state(case)
    with pytest.raises(ValidationError, match="revision base"):
        case.handler.handle(case.unit, case.command)
    assert _repair_state(case) == before


def test_repair_planning_context_requires_type_exact_planner_agent_pin():
    case = _failed_context_repair_case(planner_revision=1)
    assembly = case.unit.context_assemblies.stored
    pin = {**assembly.budget["planner_agent_ref"], "revision": True}
    case.unit.context_assemblies.stored = replace(
        assembly, budget={**assembly.budget, "planner_agent_ref": pin}
    )
    before = _repair_state(case)
    with pytest.raises(ValidationError, match="assembly policy"):
        case.handler.handle(case.unit, case.command)
    assert _repair_state(case) == before


@pytest.mark.parametrize(
    "damage",
    [
        "actor_type",
        "actor_id",
        "local_type",
        "local_id",
        "trusted_type",
        "trusted_id",
        "missing_identity",
        "denied_grant",
        "wrong_target",
        "missing_target",
        "foreign_workspace",
        "profile_resolution",
    ],
)
def test_repair_planning_context_requires_exact_human_and_saved_scope(damage):
    case = _failed_context_repair_case(deny_grant=damage == "denied_grant")
    if damage in {
        "actor_type",
        "actor_id",
        "wrong_target",
        "missing_target",
        "foreign_workspace",
    }:
        key, value = {
            "actor_type": ("actor_type", "agent"),
            "actor_id": ("actor_id", "foreign-principal"),
            "wrong_target": ("target_type", "planning_request"),
            "missing_target": ("target_id", None),
            "foreign_workspace": ("workspace_id", uuid4()),
        }[damage]
        case.command = replace(case.command, **{key: value})
    elif damage in {"local_type", "local_id"}:
        setattr(
            case.handler,
            "_local_actor_type" if damage == "local_type" else "_local_actor_id",
            "local" if damage == "local_type" else "foreign-principal",
        )
    elif damage in {"trusted_type", "trusted_id"}:
        case.handler._identity = replace(
            case.identity,
            **(
                {"actor_type": "system"}
                if damage == "trusted_type"
                else {"principal_id": "foreign-principal"}
            ),
        )
    elif damage == "missing_identity":
        case.handler._identity = None
    elif damage == "profile_resolution":

        def unavailable(*args):
            raise AssignmentDenied("Profile is unavailable")

        case.handler._assignments = SimpleNamespace(resolve_profile=unavailable)
    before = _repair_state(case)
    with pytest.raises(
        (OperatorRequired, AssignmentDenied, ValidationError, NotFound)
    ) as denied:
        case.handler.handle(case.unit, case.command)
    assert "unsupported" not in str(denied.value)
    assert _repair_state(case) == before


def test_repair_planning_context_command_service_replays_same_result_and_conflicts_on_changed_body():
    from tests.unit.test_command_service import FakeCommandRepository, FakeUnitOfWork

    case = _failed_context_repair_case()
    uow = FakeUnitOfWork(FakeCommandRepository())
    vars(uow).update(vars(case.unit))
    service = CommandService(
        lambda: uow, (case.handler,), preflight=case.handler.preflight
    )
    first = service.handle(case.command)
    after = _repair_state(case)
    assert service.handle(case.command) == first
    assert _repair_state(case) == after
    assert len(uow.commands.records) == 1 and uow.savepoint_calls == 1
    with pytest.raises(IdempotencyConflict):
        service.handle(
            replace(
                case.command,
                payload={**case.command.payload, "work_context_content_hash": "f" * 64},
            )
        )
    assert _repair_state(case) == after


def test_repair_planning_context_new_context_reaches_worker_and_strict_source_reader():
    case = _failed_context_repair_case()
    case.handler.handle(case.unit, case.command)
    assembly = case.unit.context_assemblies.added[-1]
    current = case.unit.work_contexts.revisions[assembly.work_context_revision_id]

    def unexpected_io(*args, **kwargs):
        raise AssertionError("Context repair regression must not perform external I/O")

    catalog = SimpleNamespace(materialize=unexpected_io, search=unexpected_io)
    worker = ContextPackageWorker(uow_factory=unexpected_io, catalog=catalog)
    facts = worker._planning_facts(case.unit, assembly)
    manifest = ContextPackageManifestV1.create(
        package_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=case.request.current_run_id,
        worker_item_id=None,
        work_context_revision_id=current.revision_id,
        context_version=None,
        control_plane_fact_snapshot=facts,
        knowledge_refs=(),
        artifact_refs=current.snapshot.artifact_refs,
        evidence_refs=current.snapshot.evidence_refs,
        budget=ContextBudgetV1(
            UUID(assembly.budget["profile_id"]),
            assembly.budget["profile_version"],
            assembly.budget["max_sections"],
            assembly.budget["max_chars"],
            assembly.budget["max_input_tokens"],
        ),
        expires_at=assembly.expires_at,
        idempotency_key=assembly.idempotency_key,
        created_at=assembly.created_at,
    )
    consumer = SimpleNamespace(
        work_contexts=case.unit.work_contexts,
        context_assemblies=SimpleNamespace(
            get_for_consumer=lambda *args: replace(
                assembly, status=ContextAssemblyStatus.READY
            )
        ),
    )
    reader = PlanningContextSourceReader(
        unexpected_io,
        authorizer=SimpleNamespace(require=unexpected_io),
        materializer=ContextMaterializer(catalog),
        clock=lambda: case.repair_at,
    )
    validated = reader._facts(
        consumer,
        case.request.runs[-1],
        case.request,
        manifest,
        current,
        include_bodies=False,
    )
    assert validated.revision.brief_revision == 2
    assert facts["brief"]["user_feedback"] == "Split implementation and verification."
    with pytest.raises(AgentError) as denied:
        reader._facts(
            consumer,
            case.request.runs[-1],
            case.request,
            manifest,
            case.revision,
            include_bodies=False,
        )
    assert denied.value.code == "permission_denied"


def _repair_command_request(**overrides):
    values = {
        "command_id": uuid4(),
        "workspace_id": WORKSPACE_ID,
        "command_type": "RepairPlanningContext",
        "target_type": "planning_run",
        "actor": ActorRequest(type="user", id="operator"),
        "target_id": uuid4(),
        "expected_version": 4,
        "idempotency_key": uuid4().hex,
        "correlation_id": uuid4(),
        "payload": {
            "expected_planning_request_version": 7,
            "expected_work_context_version": 3,
            "work_context_revision_id": str(uuid4()),
            "work_context_content_hash": "a" * 64,
        },
    }
    values.update(overrides)
    return CommandRequest(**values)


def test_repair_planning_context_schema_accepts_exact_payload_and_target():
    request = _repair_command_request()
    assert request.target_type == "planning_run" and request.expected_version == 4
    assert request.payload["expected_planning_request_version"] == 7
    assert request.payload["expected_work_context_version"] == 3
    assert set(request.payload) == {
        "expected_planning_request_version",
        "expected_work_context_version",
        "work_context_revision_id",
        "work_context_content_hash",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_planning_request_version", True),
        ("expected_planning_request_version", "7"),
        ("expected_planning_request_version", 0),
        ("expected_work_context_version", False),
        ("expected_work_context_version", 0),
        ("expected_work_context_version", "3"),
        ("work_context_revision_id", "not-a-uuid"),
        ("work_context_content_hash", "A" * 64),
        ("work_context_content_hash", "a" * 63),
        ("work_context_content_hash", "g" * 64),
        ("unexpected", True),
    ],
)
def test_repair_planning_context_schema_rejects_malformed_or_extra_fields(field, value):
    payload = {
        "expected_planning_request_version": 7,
        "expected_work_context_version": 3,
        "work_context_revision_id": str(uuid4()),
        "work_context_content_hash": "a" * 64,
        field: value,
    }
    with pytest.raises(ValueError):
        _repair_command_request(payload=payload)


@pytest.mark.parametrize(
    "field",
    [
        "expected_planning_request_version",
        "expected_work_context_version",
        "work_context_revision_id",
        "work_context_content_hash",
    ],
)
def test_repair_planning_context_schema_requires_every_field(field):
    payload = {
        "expected_planning_request_version": 7,
        "expected_work_context_version": 3,
        "work_context_revision_id": str(uuid4()),
        "work_context_content_hash": "a" * 64,
    }
    del payload[field]
    with pytest.raises(ValueError):
        _repair_command_request(payload=payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_type": "planning_request"},
        {"target_id": None},
        {"target_id": "invalid"},
        {"expected_version": None},
        {"expected_version": 0},
        {"expected_version": -1},
        {"expected_version": True},
        {"expected_version": "4"},
    ],
)
def test_repair_planning_context_schema_requires_exact_run_and_strict_version(
    overrides,
):
    with pytest.raises(ValueError):
        _repair_command_request(**overrides)


class FailedFeedbackRunRepository(RunRepository):
    def __init__(self, request, failed_run):
        super().__init__(request)
        self.failed_id = failed_run.id
        self.failed_row = failed_run
        self.reads = []

    def get(self, run_id, workspace_id, *, for_update=False):
        self.reads.append((run_id, workspace_id, for_update))
        if run_id == self.failed_id:
            return self.failed_row
        return super().get(run_id, workspace_id, for_update=for_update)


class FailedFeedbackContextRepository(WorkContextRepository):
    def __init__(self, existing):
        super().__init__()
        self.context = existing.context
        self.revisions = dict(existing.revisions)
        self.revision_reads = []

    def get_revision(self, revision_id, workspace_id, *, for_update=False):
        self.revision_reads.append((revision_id, workspace_id, for_update))
        return self.revisions.get(revision_id)


class FailedFeedbackAuthorityRepository:
    def __init__(self, authority, link):
        self.authority = authority
        self.link = link
        self.reads = []
        self.link_reads = []
        self.added = []
        self.saved = []
        self.revoked = []

    def get(self, authority_id, *, workspace_id=None):
        self.reads.append((authority_id, workspace_id))
        return self.authority

    def get_link(self, workspace_id, planning_run_id, *, for_update=False):
        self.link_reads.append((workspace_id, planning_run_id, for_update))
        return self.link


def _failed_planning_feedback_case(
    *, revision=False, linked_root=False, planner_revision=3
):
    case = _failed_context_repair_case(
        revision=revision, planner_revision=planner_revision
    )
    uow, request = case.unit, case.request
    context = uow.work_contexts.context
    previous = uow.work_contexts.revisions[context.current_revision_id]
    aligned = WorkContextRevisionFactory().create(
        work_context_id=context.id,
        workspace_id=WORKSPACE_ID,
        brief=request.briefs[-1],
        revision_number=context.current_revision_number + 1,
        reason=(
            WorkContextRevisionReason.REPLANNED
            if revision
            else WorkContextRevisionReason.USER_INTERVENTION
        ),
        actor="operator",
        created_at=NOW,
        base_snapshot=previous.snapshot,
        plan_snapshot_id=previous.plan_snapshot_id,
    )
    previous_version = context.version
    append = context.append_external_revision if revision else context.append_revision
    append(aligned, expected_version=previous_version, now=NOW)
    uow.work_contexts.append_revision(
        context, aligned, expected_version=previous_version
    )
    if linked_root:
        context.bind_root_task(
            case.goal.root_task_id, expected_version=context.version, now=NOW
        )
    uow.work_contexts = FailedFeedbackContextRepository(uow.work_contexts)
    uow.planning_runs = FailedFeedbackRunRepository(request, case.run)
    workspace = ExecutionWorkspace(
        directory="/tmp/sidera-failed-feedback-original",
        writable=True,
        artifact_directory=None,
    )
    authority = PlanningAuthority(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        planning_request_id=request.id,
        planning_run_id=case.run.id,
        planning_profile_id=case.profile.id,
        planning_profile_version=case.profile.version,
        operation_id=case.run.operation_id,
        principal_id=case.identity.principal_id,
        upstream_grant_id=uuid4(),
        profile_ref=case.profile.planner_agent_ref,
        runtime_ref=RuntimeRef(workspace_id=WORKSPACE_ID, runtime_id=uuid4()),
        lease_id=uuid4(),
        owner_id="original-owner",
        fencing_token=1,
        execution_workspace=workspace,
        actions=("run.start", "run.read", "source.read"),
        revoked=True,
    )
    start = RunStart(
        operation_id=case.run.operation_id,
        profile_ref=authority.profile_ref,
        grant_id=authority.id,
        policy="bounded",
        instructions=("Return a strict planning proposal.",),
        text=request.briefs[-1].objective,
        context=(),
        tools=(),
        workspace=workspace,
        required_capabilities=(),
    )
    native_run_id = uuid4()
    link = PlanningAgentLink(
        workspace_id=WORKSPACE_ID,
        planning_run_id=case.run.id,
        authority_id=authority.id,
        start_operation_id=case.run.operation_id,
        profile_ref=authority.profile_ref,
        start_request=start,
        request_sha256=request_fingerprint(start),
        status="closed",
        run_id=native_run_id,
        start_receipt=StartReceipt(
            operation_id=case.run.operation_id,
            status=StartStatus.ACCEPTED,
            run_id=native_run_id,
            reason_code=None,
        ),
        event_cursor=7,
        response_id=uuid4(),
        closed=True,
    )
    uow.planning_authorities = FailedFeedbackAuthorityRepository(authority, link)
    uow.context_assemblies.stored = replace(
        build_planning_context_assembly_request(
            run=case.run,
            brief=request.briefs[-1],
            work_context_revision=aligned,
            ttl_seconds=1800,
            created_at=NOW,
            planning_kind=request.kind,
            revision_base=request.base_descriptor,
        ),
        status=ContextAssemblyStatus.READY,
    )
    case.revision = aligned
    case.access = SimpleNamespace(grant_valid=True, profile_readable=True)
    case.goal_reads = []

    def get_goal(workspace_id, planning_request_id, *, for_update=False):
        case.goal_reads.append((workspace_id, planning_request_id, for_update))
        return uow.task_goals.submission

    uow.task_goals = SimpleNamespace(get=get_goal, submission=case.goal)

    def require(grant_id, action, target):
        case.grant_reads.append((grant_id, action, target))
        assert (grant_id, action) == (case.identity.grant_id, "planning.write")
        assert target == PlanningCommandTarget(
            WORKSPACE_ID,
            "operator",
            request.id,
            case.profile.id,
            case.profile.version,
            case.profile.planner_agent_ref,
            None,
            request.briefs[-1].context_refs,
            request.root_task_id if revision else case.goal.root_task_id,
        )
        if not case.access.grant_valid:
            raise AgentError("permission_denied", "Current grant is unavailable")

    def resolve_profile(workspace_id, profile_id, profile_revision):
        assert (workspace_id, profile_id, profile_revision) == (
            WORKSPACE_ID,
            case.profile.planner_agent_ref.profile_id,
            case.profile.planner_agent_ref.revision,
        )
        case.profile_reads.append((workspace_id, profile_id, profile_revision))
        if not case.access.profile_readable:
            raise AssignmentDenied("Saved Profile is disabled or unreadable")
        return case.profile.planner_agent_ref

    case.handler._authorizer = SimpleNamespace(require=require)
    case.handler._assignments = SimpleNamespace(resolve_profile=resolve_profile)
    case.command = envelope(
        "ReviseFailedPlanningRequest",
        {
            "failed_planning_run_id": str(case.run.id),
            "expected_failed_planning_run_version": case.run.version,
            "expected_work_context_version": context.version,
            "work_context_revision_id": str(aligned.revision_id),
            "work_context_content_hash": aligned.content_hash,
            "feedback": "  Keep four workers and explicit review. \n",
        },
        actor_type="user",
        target_type="planning_request",
        target_id=request.id,
        expected_version=request.version,
    )
    uow.work_contexts.added.clear()
    uow.work_contexts.appended.clear()
    return case


def _failed_feedback_state(case):
    authorities = case.unit.planning_authorities
    return _failed_feedback_values(
        (
            _repair_state(case),
            case.unit.planning_runs.failed_row,
            authorities.authority,
            authorities.added,
            authorities.saved,
            authorities.revoked,
        )
    )


def _failed_feedback_values(value):
    if isinstance(value, BaseModel):
        return _failed_feedback_values(value.model_dump(mode="python", warnings=False))
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _failed_feedback_values(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: _failed_feedback_values(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_failed_feedback_values(item) for item in value)
    if isinstance(value, SimpleNamespace):
        return _failed_feedback_values(vars(value))
    return deepcopy(value)


def _damaged_feedback_value(value, **changes):
    # Model corrupt retained rows without weakening the real typed fixture constructors.
    damaged = copy(value)
    for name, replacement in changes.items():
        object.__setattr__(damaged, name, replacement)
    return damaged


@pytest.mark.parametrize("revision", [False, True], ids=["create", "revision"])
def test_failed_planning_feedback_appends_atomic_matching_history(revision):
    case = _failed_planning_feedback_case(revision=revision, linked_root=not revision)
    uow, request = case.unit, case.request
    before = _failed_feedback_state(case)
    old_runs = tuple(replace(run) for run in request.runs)
    old_briefs = request.briefs
    old_context = replace(uow.work_contexts.context)
    old_revisions = _failed_feedback_values(uow.work_contexts.revisions)
    old_base = request.base_descriptor
    old_assembly = _damaged_feedback_value(uow.context_assemblies.stored)
    old_version = request.version

    result = case.handler.handle(uow, case.command)

    brief, successor = request.briefs[-1], request.runs[-1]
    context = uow.work_contexts.context
    current = uow.work_contexts.revisions[context.current_revision_id]
    assert request.id == case.command.target_id
    assert request.version == old_version + 1
    assert request.status is PlanningRequestStatus.PLANNING
    assert request.runs[:-1] == old_runs and request.briefs[:-1] == old_briefs
    assert (
        successor.id != case.run.id and successor.operation_id != case.run.operation_id
    )
    assert successor.attempt == case.run.attempt + 1
    assert successor.status is PlanningRunStatus.QUEUED
    assert successor.brief_revision == brief.revision == 3
    assert successor.profile_snapshot == case.run.profile_snapshot
    assert successor.profile_snapshot.retry_policy.max_retries == 0
    assert brief.user_feedback == (
        "Split implementation and verification.\n\nKeep four workers and explicit review."
    )
    for field in (
        "objective",
        "constraints",
        "acceptance_criteria",
        "context_refs",
        "requested_outputs",
    ):
        assert getattr(brief, field) == getattr(old_briefs[-1], field)
    assert request.current_proposal_revision is None
    assert request.current_proposal_content_hash is None
    assert request.base_descriptor == old_base
    assert (
        context.id,
        context.workspace_id,
        context.planning_request_id,
        context.root_task_id,
    ) == (
        old_context.id,
        old_context.workspace_id,
        old_context.planning_request_id,
        old_context.root_task_id,
    )
    assert context.version == old_context.version + 1
    assert current.revision_number == old_context.current_revision_number + 1
    assert current.brief_planning_request_id == request.id
    assert current.brief_revision == brief.revision
    assert current.content_hash == current.snapshot.content_hash
    assert current.plan_snapshot_id == case.revision.plan_snapshot_id
    assert current.reason is (
        WorkContextRevisionReason.REPLANNED
        if revision
        else WorkContextRevisionReason.USER_INTERVENTION
    )
    for field in (
        "clarification_decisions",
        "accepted_decisions",
        "unresolved_questions",
        "knowledge_refs",
        "artifact_refs",
        "evidence_refs",
        "checkpoint_refs",
        "missing_fact_codes",
    ):
        assert getattr(current.snapshot, field) == getattr(
            case.revision.snapshot, field
        )
    assert all(
        _failed_feedback_values(uow.work_contexts.revisions[key]) == value
        for key, value in old_revisions.items()
    )
    assert uow.work_contexts.appended == [(current, old_context.version)]
    assert uow.planning_records.briefs == list(request.briefs)
    assert uow.planning_runs.added == [successor] and not uow.planning_runs.saved
    assert uow.planning_requests.saved == [(request, old_version)]
    assert len(uow.context_assemblies.added) == 1
    assembly = uow.context_assemblies.added[0]
    assert (assembly.consumer_id, assembly.work_context_revision_id) == (
        successor.id,
        current.revision_id,
    )
    assert assembly.request_fingerprint != old_assembly.request_fingerprint
    assert uow.context_assemblies.stored == old_assembly
    assert _failed_feedback_values(
        (uow.planning_authorities.authority, uow.planning_authorities.link)
    ) == (before[2], before[0][4])
    assert uow.planning_authorities.authority.revoked is True
    assert (
        uow.planning_authorities.authority.upstream_grant_id != case.identity.grant_id
    )
    assert (
        uow.planning_authorities.added,
        uow.planning_authorities.saved,
        uow.planning_authorities.revoked,
    ) == ([], [], [])
    assert dict(vars(case.proposal)) == before[0][17]
    assert _failed_feedback_values(uow.task_goals.submission) == before[0][20]
    assert _failed_feedback_values(uow.tasks.task) == before[0][21]
    if revision:
        assert (
            _failed_feedback_values((uow.plans.head, uow.plans.snapshot))
            == before[0][22]
        )
    assert result.data == {
        "planning_request_id": str(request.id),
        "old_planning_run_id": str(case.run.id),
        "planning_run_id": str(successor.id),
        "attempt": successor.attempt,
        "brief_revision": brief.revision,
        "work_context_id": str(context.id),
        "work_context_revision": current.revision_number,
        "work_context_content_hash": current.content_hash,
    }
    assert [event.event_type for event in uow.events.events] == [
        "WorkContextRevised",
        "FailedPlanningRequestRevised",
    ]
    assert result.event_ids == tuple(event.id for event in uow.events.events)
    assert (case.run.id, WORKSPACE_ID, True) in uow.planning_runs.reads
    assert uow.planning_leases.reads == [(case.run.id, WORKSPACE_ID, True)]
    assert uow.planning_authorities.link_reads == [(WORKSPACE_ID, case.run.id, True)]
    assert uow.planning_authorities.reads == [(before[2]["id"], WORKSPACE_ID)]
    assert (
        case.revision.revision_id,
        WORKSPACE_ID,
        True,
    ) in uow.work_contexts.revision_reads
    assert uow.work_contexts.lookups == [
        (
            "root" if revision else "request",
            request.root_task_id if revision else request.id,
            WORKSPACE_ID,
            True,
        )
    ]


@pytest.mark.parametrize(
    "damage,error",
    [
        ("stale_request", PlanningConflict),
        ("stale_run", VersionConflict),
        ("noncurrent_run", ValidationError),
        ("request_not_failed", ValidationError),
        ("run_not_failed", ValidationError),
        ("missing_run", NotFound),
        ("foreign_run_workspace", AssignmentDenied),
        ("foreign_run_request", AssignmentDenied),
        ("run_policy", AssignmentDenied),
        ("run_brief", ValidationError),
        ("saved_policy", AssignmentDenied),
        ("saved_profile", AssignmentDenied),
        ("legacy", AssignmentDenied),
        ("pre_native", ValidationError),
        ("missing_result", ValidationError),
        ("missing_finished", ValidationError),
        ("blank_feedback", ValidationError),
        ("extra_payload", ValidationError),
        ("missing_payload", ValidationError),
        ("bool_run_version", ValidationError),
        ("bool_request_version", ValidationError),
        ("active_lease", ValidationError),
        ("expired_active_lease", ValidationError),
        ("foreign_goal", AssignmentDenied),
    ],
)
def test_failed_planning_feedback_refuses_invalid_request_run_or_lease(
    damage, error, monkeypatch
):
    case = _failed_planning_feedback_case()
    request, run, uow = case.request, case.run, case.unit
    payload = dict(case.command.payload)
    if damage == "stale_request":
        case.command = replace(case.command, expected_version=request.version - 1)
    elif damage == "stale_run":
        payload["expected_failed_planning_run_version"] = run.version - 1
    elif damage == "noncurrent_run":
        payload["failed_planning_run_id"] = str(request.runs[0].id)
    elif damage == "request_not_failed":
        request.status = PlanningRequestStatus.PLANNING
    elif damage == "run_not_failed":
        run.status = PlanningRunStatus.BLOCKED
    elif damage == "missing_run":
        uow.planning_runs.failed_row = None
    elif damage in {
        "foreign_run_workspace",
        "foreign_run_request",
        "run_policy",
        "run_brief",
        "pre_native",
        "missing_result",
        "missing_finished",
    }:
        name, value = {
            "foreign_run_workspace": ("workspace_id", uuid4()),
            "foreign_run_request": ("planning_request_id", uuid4()),
            "run_policy": (
                "profile_snapshot",
                replace(run.profile_snapshot, profile_id=uuid4()),
            ),
            "run_brief": ("brief_revision", 1),
            "pre_native": ("provider_ref", None),
            "missing_result": ("structured_result_type", None),
            "missing_finished": ("finished_at", None),
        }[damage]
        setattr(run, name, value)
    elif damage == "saved_policy":
        request.planning_profile_id = uuid4()
    elif damage == "saved_profile":
        request.profile_snapshot = _damaged_feedback_value(
            request.profile_snapshot, planner_agent_ref=None
        )
    elif damage == "legacy":
        request.profile_snapshot = _damaged_feedback_value(
            request.profile_snapshot, execution_mode="legacy"
        )
    elif damage == "blank_feedback":
        payload["feedback"] = " \n "
    elif damage == "extra_payload":
        payload["closed"] = True
    elif damage == "missing_payload":
        del payload["feedback"]
    elif damage == "bool_run_version":
        payload["expected_failed_planning_run_version"] = True
    elif damage == "bool_request_version":
        case.command = replace(case.command, expected_version=True)
    elif damage == "foreign_goal":
        uow.task_goals.submission = replace(case.goal, planning_request_id=uuid4())
    else:
        uow.planning_leases.active = PlanningRunLease(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            planning_run_id=run.id,
            owner_id="retained-owner",
            fencing_token=1,
            status=PlanningRunLeaseStatus.ACTIVE,
            acquired_at=NOW,
            expires_at=NOW
            + timedelta(seconds=-1 if damage == "expired_active_lease" else 300),
            released_at=None,
            version=1,
        )
    case.command = replace(case.command, payload=payload)
    before = _failed_feedback_state(case)
    monkeypatch.setattr(
        "sidera_control_plane.application.planning_commands.uuid4",
        lambda: pytest.fail("allocated an ID before refusal"),
    )
    with pytest.raises(error) as refused:
        case.handler.handle(uow, case.command)
    assert "unsupported" not in str(refused.value)
    assert _failed_feedback_state(case) == before


@pytest.mark.parametrize(
    "damage",
    [
        "no_link",
        "untyped_link",
        "pending",
        "starting",
        "unknown",
        "cleanup_pending",
        "waiting_capacity",
        "rejected",
        "running",
        "not_admitted",
        "closed_false",
        "no_native_run",
        "no_response",
        "zero_cursor",
        "bool_cursor",
        "no_receipt",
        "untyped_receipt",
        "rejected_receipt",
        "receipt_operation",
        "receipt_run",
        "link_operation",
        "link_run",
        "link_workspace",
        "link_profile",
        "start_operation",
        "start_profile",
        "start_grant",
        "bad_fingerprint",
        "untyped_start",
        "oversized_start",
        "oversized_instructions",
        "oversized_context",
        "oversized_tokens",
        "unbounded_start",
        "missing_authority",
    ],
)
def test_failed_planning_feedback_requires_proven_typed_original_closure(
    damage, monkeypatch
):
    case = _failed_planning_feedback_case()
    repository = case.unit.planning_authorities
    link = repository.link
    if damage == "no_link":
        repository.link = None
    elif damage == "untyped_link":
        repository.link = SimpleNamespace(
            **{name: getattr(link, name) for name in link.__dataclass_fields__}
        )
    elif damage in {
        "pending",
        "starting",
        "unknown",
        "cleanup_pending",
        "waiting_capacity",
        "rejected",
        "running",
        "not_admitted",
    }:
        repository.link = _damaged_feedback_value(link, status=damage, closed=False)
    elif damage == "missing_authority":
        repository.authority = None
    elif damage in {
        "untyped_start",
        "start_operation",
        "start_profile",
        "start_grant",
        "oversized_start",
        "oversized_instructions",
        "oversized_context",
        "oversized_tokens",
        "unbounded_start",
    }:
        start = link.start_request
        updates = {
            "start_operation": {"operation_id": uuid4()},
            "start_profile": {
                "profile_ref": start.profile_ref.model_copy(
                    update={"workspace_id": uuid4()}
                )
            },
            "start_grant": {"grant_id": uuid4()},
            "oversized_start": {
                "text": "x" * (case.profile.budget.max_context_bytes + 1)
            },
            "oversized_instructions": {
                "instructions": ("x" * (case.profile.budget.max_context_bytes + 1),)
            },
            "oversized_context": {
                "context": (
                    ContextItem(
                        item_id="large",
                        origin="test",
                        required=True,
                        ordinal=0,
                        version="1",
                        content_hash="a" * 64,
                        text="x" * (case.profile.budget.max_context_bytes + 1),
                    ),
                )
            },
            "oversized_tokens": {
                "text": "x" * (case.profile.budget.max_tokens * 4 + 1)
            },
            "unbounded_start": {"policy": RunPolicy.CONVERSATION},
            "untyped_start": {},
        }[damage]
        start = None if damage == "untyped_start" else start.model_copy(update=updates)
        repository.link = _damaged_feedback_value(
            link,
            start_request=start,
            request_sha256=link.request_sha256
            if start is None
            else request_fingerprint(start),
        )
    elif damage in {
        "untyped_receipt",
        "rejected_receipt",
        "receipt_operation",
        "receipt_run",
    }:
        receipt = link.start_receipt
        receipt = (
            None
            if damage == "untyped_receipt"
            else receipt.model_copy(
                update={
                    "rejected_receipt": {"status": StartStatus.REJECTED},
                    "receipt_operation": {"operation_id": uuid4()},
                    "receipt_run": {"run_id": uuid4()},
                }[damage]
            )
        )
        if damage == "untyped_receipt":
            receipt = SimpleNamespace(
                status=StartStatus.ACCEPTED,
                operation_id=link.start_operation_id,
                run_id=link.run_id,
            )
        repository.link = _damaged_feedback_value(link, start_receipt=receipt)
    else:
        name, value = {
            "closed_false": ("closed", False),
            "no_native_run": ("run_id", None),
            "no_response": ("response_id", None),
            "zero_cursor": ("event_cursor", 0),
            "bool_cursor": ("event_cursor", True),
            "no_receipt": ("start_receipt", None),
            "link_operation": ("start_operation_id", uuid4()),
            "link_run": ("planning_run_id", uuid4()),
            "link_workspace": ("workspace_id", uuid4()),
            "link_profile": (
                "profile_ref",
                link.profile_ref.model_copy(update={"profile_id": uuid4()}),
            ),
            "bad_fingerprint": ("request_sha256", "f" * 64),
        }[damage]
        repository.link = _damaged_feedback_value(link, **{name: value})
    before = _failed_feedback_state(case)
    monkeypatch.setattr(
        "sidera_control_plane.application.planning_commands.uuid4",
        lambda: pytest.fail("allocated an ID before closure validation"),
    )
    with pytest.raises(NativePlanningCleanupPending):
        case.handler.handle(case.unit, case.command)
    assert _failed_feedback_state(case) == before


@pytest.mark.parametrize(
    "damage",
    [
        "untyped",
        "id",
        "workspace_id",
        "planning_request_id",
        "planning_run_id",
        "planning_profile_id",
        "planning_profile_version",
        "operation_id",
        "principal_id",
        "profile_ref",
        "runtime_ref",
        "execution_workspace",
        "bool_policy_version",
        "bool_profile_version",
    ],
)
def test_failed_planning_feedback_refuses_mismatched_historical_authority(damage):
    case = _failed_planning_feedback_case(
        planner_revision=1 if damage == "bool_profile_version" else 3
    )
    repository = case.unit.planning_authorities
    authority = repository.authority
    if damage == "untyped":
        repository.authority = SimpleNamespace(
            **{
                name: getattr(authority, name)
                for name in authority.__dataclass_fields__
            }
        )
    else:
        name, value = {
            "id": ("id", uuid4()),
            "workspace_id": ("workspace_id", uuid4()),
            "planning_request_id": ("planning_request_id", uuid4()),
            "planning_run_id": ("planning_run_id", uuid4()),
            "planning_profile_id": ("planning_profile_id", uuid4()),
            "planning_profile_version": ("planning_profile_version", 2),
            "operation_id": ("operation_id", uuid4()),
            "principal_id": ("principal_id", "foreign"),
            "profile_ref": (
                "profile_ref",
                authority.profile_ref.model_copy(update={"profile_id": uuid4()}),
            ),
            "runtime_ref": (
                "runtime_ref",
                authority.runtime_ref.model_copy(update={"workspace_id": uuid4()}),
            ),
            "execution_workspace": (
                "execution_workspace",
                authority.execution_workspace.model_copy(
                    update={"directory": "/tmp/foreign"}
                ),
            ),
            "bool_policy_version": ("planning_profile_version", True),
            "bool_profile_version": (
                "profile_ref",
                authority.profile_ref.model_copy(update={"revision": True}),
            ),
        }[damage]
        repository.authority = _damaged_feedback_value(authority, **{name: value})
    before = _failed_feedback_state(case)
    with pytest.raises(AssignmentDenied):
        case.handler.handle(case.unit, case.command)
    assert _failed_feedback_state(case) == before


@pytest.mark.parametrize("revision", [False, True], ids=["create", "revision"])
@pytest.mark.parametrize(
    "damage,error",
    [
        ("missing_context", NotFound),
        ("inactive", ValidationError),
        ("wrong_owner", (NotFound, ValidationError)),
        ("foreign_root", (NotFound, ValidationError)),
        ("context_version", VersionConflict),
        ("revision_id", ValidationError),
        ("missing_revision", NotFound),
        ("untyped_revision", ValidationError),
        ("revision_context", ValidationError),
        ("revision_workspace", ValidationError),
        ("revision_number", ValidationError),
        ("submitted_hash", ValidationError),
        ("snapshot_hash", ValidationError),
        ("stale_brief", ValidationError),
        ("foreign_brief", ValidationError),
        ("request_owned_fallback", (NotFound, ValidationError)),
        ("stored_revision_id", ValidationError),
    ],
)
def test_failed_planning_feedback_requires_aligned_owned_current_context(
    revision, damage, error
):
    case = _failed_planning_feedback_case(revision=revision)
    repository = case.unit.work_contexts
    context, current = repository.context, case.revision
    payload = dict(case.command.payload)
    if damage == "missing_context":
        repository.context = None
    elif damage == "inactive":
        context.status = WorkContextStatus.ARCHIVED
    elif damage == "wrong_owner":
        context.planning_request_id = case.request.id if revision else uuid4()
    elif damage == "foreign_root":
        context.root_task_id = uuid4()
    elif damage == "context_version":
        payload["expected_work_context_version"] -= 1
    elif damage == "revision_id":
        payload["work_context_revision_id"] = str(uuid4())
    elif damage == "missing_revision":
        del repository.revisions[current.revision_id]
    elif damage == "untyped_revision":
        repository.revisions[current.revision_id] = SimpleNamespace(
            **{name: getattr(current, name) for name in current.__dataclass_fields__}
        )
    elif damage == "submitted_hash":
        payload["work_context_content_hash"] = "f" * 64
    elif damage == "request_owned_fallback":
        context.planning_request_id = case.request.id
        context.root_task_id = None if revision else uuid4()
    else:
        name, value = {
            "revision_context": ("work_context_id", uuid4()),
            "revision_workspace": ("workspace_id", uuid4()),
            "revision_number": ("revision_number", current.revision_number + 1),
            "snapshot_hash": (
                "snapshot",
                replace(current.snapshot, objective="Unexpected altered snapshot"),
            ),
            "stale_brief": ("brief_revision", current.brief_revision - 1),
            "foreign_brief": ("brief_planning_request_id", uuid4()),
            "stored_revision_id": ("revision_id", uuid4()),
        }[damage]
        repository.revisions[current.revision_id] = _damaged_feedback_value(
            current, **{name: value}
        )
    case.command = replace(case.command, payload=payload)
    before = _failed_feedback_state(case)
    with pytest.raises(error) as refused:
        case.handler.handle(case.unit, case.command)
    assert "unsupported" not in str(refused.value)
    assert _failed_feedback_state(case) == before
    if revision:
        assert all(read[0] == "root" for read in repository.lookups)


def test_failed_planning_feedback_revision_preserves_exact_original_base_snapshot():
    case = _failed_planning_feedback_case(revision=True)
    current = case.revision
    case.unit.work_contexts.revisions[current.revision_id] = _damaged_feedback_value(
        current, plan_snapshot_id=uuid4()
    )
    before = _failed_feedback_state(case)
    with pytest.raises(ValidationError):
        case.handler.handle(case.unit, case.command)
    assert _failed_feedback_state(case) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("failed_planning_run_id", "invalid"),
        ("work_context_revision_id", "invalid"),
        ("work_context_content_hash", "A" * 64),
        ("feedback", None),
        ("closed", True),
    ]
    + [
        (field, value)
        for field in (
            "expected_failed_planning_run_version",
            "expected_work_context_version",
        )
        for value in (True, "2", 2.0, 0, -1)
    ],
)
def test_failed_planning_feedback_direct_handler_rejects_malformed_pins(field, value):
    case = _failed_planning_feedback_case()
    case.command = replace(case.command, payload={**case.command.payload, field: value})
    before = _failed_feedback_state(case)
    with pytest.raises(ValidationError) as refused:
        case.handler.handle(case.unit, case.command)
    assert "unsupported" not in str(refused.value)
    assert _failed_feedback_state(case) == before


def _failed_feedback_service(case):
    from contextlib import contextmanager

    from tests.unit.test_command_service import FakeCommandRepository, FakeUnitOfWork

    class Commands(FakeCommandRepository):
        def __init__(self):
            super().__init__()
            self.lookups = []

        def find_by_id(self, workspace_id, command_id):
            self.lookups.append((workspace_id, command_id))
            return super().find_by_id(workspace_id, command_id)

    class Unit(FakeUnitOfWork):
        active_savepoint = False

        @contextmanager
        def savepoint(self):
            with super().savepoint():
                self.active_savepoint = True
                try:
                    yield
                finally:
                    self.active_savepoint = False

    uow = Unit(Commands())
    vars(uow).update(vars(case.unit))
    case.unit = uow
    uow.boundary_writes = []

    def guarded(owner, method):
        original = getattr(owner, method)

        def write(*args, **kwargs):
            assert uow.active_savepoint
            uow.boundary_writes.append(method)
            return original(*args, **kwargs)

        setattr(owner, method, write)

    for owner, method in (
        (uow.planning_records, "add_brief"),
        (uow.work_contexts, "append_revision"),
        (uow.planning_runs, "add"),
        (uow.context_assemblies, "add"),
        (uow.planning_requests, "save"),
        (uow.events, "append_with_outbox"),
        (uow.commands, "record_applied"),
    ):
        guarded(owner, method)
    return CommandService(
        lambda: uow, (case.handler,), preflight=case.handler.preflight
    )


def test_failed_planning_feedback_service_replays_after_success_and_preserves_savepoint_boundary():
    case = _failed_planning_feedback_case()
    service = _failed_feedback_service(case)
    first = service.handle(case.command)
    before = _failed_feedback_state(case)
    writes = tuple(case.unit.boundary_writes)
    assert case.request.status is PlanningRequestStatus.PLANNING
    assert service.handle(case.command) == first
    assert _failed_feedback_state(case) == before
    assert tuple(case.unit.boundary_writes) == writes
    assert writes == (
        "add_brief",
        "append_revision",
        "add",
        "add",
        "save",
        "append_with_outbox",
        "append_with_outbox",
        "record_applied",
    )
    assert case.unit.savepoint_calls == 1
    assert len(case.unit.commands.records) == 1
    with pytest.raises(IdempotencyConflict):
        service.handle(
            replace(
                case.command,
                payload={**case.command.payload, "feedback": "Different requirements"},
            )
        )
    assert _failed_feedback_state(case) == before
    assert tuple(case.unit.boundary_writes) == writes


def test_failed_planning_feedback_replay_accepts_fresh_current_session_grant():
    case = _failed_planning_feedback_case()
    service = _failed_feedback_service(case)
    first = service.handle(case.command)
    before = _failed_feedback_state(case)
    old_grant_id = case.identity.grant_id
    case.identity = replace(case.identity, grant_id=uuid4())
    case.handler._identity = case.identity
    assert case.identity.grant_id != old_grant_id
    assert service.handle(case.command) == first
    assert _failed_feedback_state(case) == before
    assert case.grant_reads[-1][0] == case.identity.grant_id
    assert case.unit.savepoint_calls == 1


@pytest.mark.parametrize("applied", [False, True], ids=["first", "replay"])
@pytest.mark.parametrize(
    "damage",
    [
        "foreign_actor",
        "foreign_workspace",
        "agent_actor",
        "missing_identity",
        "foreign_identity",
        "system_identity",
        "missing_grant",
        "revoked_grant",
        "disabled_profile",
        "unreadable_profile",
        "wrong_target",
        "missing_target",
        "local_type",
        "local_id",
    ],
)
def test_failed_planning_feedback_authorizes_current_human_before_receipt_lookup(
    applied, damage
):
    case = _failed_planning_feedback_case()
    service = _failed_feedback_service(case)
    if applied:
        service.handle(case.command)
    if damage in {
        "foreign_actor",
        "foreign_workspace",
        "agent_actor",
        "wrong_target",
        "missing_target",
    }:
        case.command = replace(
            case.command,
            **{
                "foreign_actor": {"actor_id": "foreign"},
                "foreign_workspace": {"workspace_id": uuid4()},
                "agent_actor": {"actor_type": "agent"},
                "wrong_target": {"target_type": "planning_run"},
                "missing_target": {"target_id": None},
            }[damage],
        )
    elif damage == "missing_identity":
        case.handler._identity = None
    elif damage in {"foreign_identity", "system_identity"}:
        case.handler._identity = replace(
            case.identity,
            **(
                {"principal_id": "foreign"}
                if damage == "foreign_identity"
                else {"actor_type": "system"}
            ),
        )
    elif damage in {"missing_grant", "revoked_grant"}:
        case.access.grant_valid = False
    elif damage in {"local_type", "local_id"}:
        setattr(
            case.handler,
            "_local_actor_type" if damage == "local_type" else "_local_actor_id",
            "system" if damage == "local_type" else "foreign",
        )
    else:
        case.access.profile_readable = False
    before = _failed_feedback_state(case)
    receipts = deepcopy(case.unit.commands.records)
    lookups = tuple(case.unit.commands.lookups)
    with pytest.raises(
        (AssignmentDenied, OperatorRequired, NotFound, ValidationError)
    ) as refused:
        service.handle(case.command)
    assert "unsupported" not in str(refused.value)
    assert _failed_feedback_state(case) == before
    assert case.unit.commands.records == receipts
    assert tuple(case.unit.commands.lookups) == lookups


def test_failed_planning_feedback_new_context_reaches_worker_and_strict_source_reader():
    from sidera_control_plane.application.planning_runner import (
        PlanningRunner,
        _ClaimedRun,
    )

    case = _failed_planning_feedback_case()
    original_assembly = _damaged_feedback_value(case.unit.context_assemblies.stored)

    def unexpected_io(*args, **kwargs):
        pytest.fail("Failed-feedback source verification performed external I/O")

    catalog = SimpleNamespace(materialize=unexpected_io, search=unexpected_io)
    producer = ContextPackageWorker(uow_factory=unexpected_io, catalog=catalog)
    old_facts = deepcopy(producer._planning_facts(case.unit, original_assembly))
    case.handler.handle(case.unit, case.command)
    assembly = case.unit.context_assemblies.added[-1]
    current = case.unit.work_contexts.revisions[assembly.work_context_revision_id]
    facts = producer._planning_facts(case.unit, assembly)
    manifest = ContextPackageManifestV1.create(
        package_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        purpose=assembly.purpose,
        consumer_type=assembly.consumer_type,
        consumer_id=assembly.consumer_id,
        worker_item_id=None,
        work_context_revision_id=current.revision_id,
        context_version=None,
        control_plane_fact_snapshot=facts,
        knowledge_refs=(),
        artifact_refs=current.snapshot.artifact_refs,
        evidence_refs=current.snapshot.evidence_refs,
        budget=ContextBudgetV1(
            UUID(assembly.budget["profile_id"]),
            assembly.budget["profile_version"],
            assembly.budget["max_sections"],
            assembly.budget["max_chars"],
            assembly.budget["max_input_tokens"],
        ),
        expires_at=assembly.expires_at,
        idempotency_key=assembly.idempotency_key,
        created_at=assembly.created_at,
    )
    consumer = SimpleNamespace(
        work_contexts=case.unit.work_contexts,
        context_assemblies=SimpleNamespace(
            get_for_consumer=lambda *args: replace(
                assembly, status=ContextAssemblyStatus.READY
            )
        ),
    )
    reader = PlanningContextSourceReader(
        unexpected_io,
        authorizer=SimpleNamespace(require=unexpected_io),
        materializer=ContextMaterializer(catalog),
        clock=lambda: case.repair_at,
    )
    validated = reader._facts(
        consumer,
        case.request.runs[-1],
        case.request,
        manifest,
        current,
        include_bodies=False,
    )
    assert validated.revision.brief_revision == 3
    assert (
        facts["brief"]["user_feedback"]
        == "Split implementation and verification.\n\nKeep four workers and explicit review."
    )
    assert case.unit.context_assemblies.stored == original_assembly
    assert old_facts["brief"]["revision"] == 2
    assert (
        old_facts["brief"]["user_feedback"] == "Split implementation and verification."
    )
    assert case.unit.work_contexts.revisions[case.revision.revision_id] == case.revision
    successor = case.request.runs[-1]
    claimed = _ClaimedRun(
        run_id=successor.id,
        request_id=case.request.id,
        workspace_id=WORKSPACE_ID,
        operation_id=successor.operation_id,
        lease_id=uuid4(),
        fencing_token=successor.attempt,
        attempt=successor.attempt,
        brief=case.request.briefs[-1],
        profile_snapshot=successor.profile_snapshot,
        correlation_id=case.request.correlation_id,
        definition=None,
        instance=None,
        context_package=manifest,
        context_revision=current,
    )
    produced_input = PlanningRunner._assemble_input(claimed, case.repair_at)
    assert produced_input.brief_revision == 3
    assert produced_input.brief.user_feedback == facts["brief"]["user_feedback"]
    assert produced_input.planning_run_id == assembly.consumer_id
    with pytest.raises(AgentError) as refused:
        reader._facts(
            consumer,
            case.request.runs[-1],
            case.request,
            manifest,
            case.revision,
            include_bodies=False,
        )
    assert refused.value.code == "permission_denied"
