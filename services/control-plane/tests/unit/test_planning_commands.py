from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.planning_commands import PlanningCommandHandler
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import PlanningRequest, PlanningRequestStatus
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("70000000-0000-0000-0000-000000000001")


def planner_capabilities() -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2,),
    )


def module_facts() -> tuple[ModuleDefinition, ModuleInstance]:
    definition_id = uuid4()
    instance_id = uuid4()
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=WORKSPACE_ID,
        module_type="planner_adapter",
        provider="fake",
        interface_version="sidera.planner.v1",
        config_schema_version=1,
        capabilities=planner_capabilities(),
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


def profile(instance_id: UUID) -> PlanningProfile:
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
        proposal_schema_version=2,
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
    def __init__(self, definition: ModuleDefinition, instance: ModuleInstance) -> None:
        self.definition = definition
        self.instance = instance
        self.added: list[object] = []

    def get_definition(self, module_id: UUID, workspace_id: UUID):
        return self.definition if module_id == self.definition.id else None

    def get_instance(self, module_id: UUID, workspace_id: UUID):
        return self.instance if module_id == self.instance.id else None

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


class RunRepository:
    def __init__(self) -> None:
        self.saved: list[tuple[object, int]] = []

    def save(self, run: object, previous_version: int) -> None:
        self.saved.append((run, previous_version))


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


def unit(
    definition: ModuleDefinition,
    instance: ModuleInstance,
    stored_profile: PlanningProfile | None = None,
    stored_request: PlanningRequest | None = None,
) -> Any:
    return SimpleNamespace(
        modules=ModuleRepository(definition, instance),
        planning_profiles=ProfileRepository(stored_profile),
        planning_requests=RequestRepository(stored_request),
        planning_runs=RunRepository(),
        planning_leases=SimpleNamespace(),
        planning_records=SimpleNamespace(),
        proposal_materializations=SimpleNamespace(),
        execution_requests=SimpleNamespace(),
        events=EventRepository(),
        projection_changes=ProjectionPublisher(),
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
        handler.handle(unit(definition, instance), envelope("RegisterPlannerModule", secret_payload))


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

    assert result.data["planning_profile_id"] == str(unit_of_work.planning_profiles.stored.id)
    assert result.data["profile_version"] == 1
    assert unit_of_work.planning_profiles.stored.planner_module_instance_id == instance.id


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
    assert unit_of_work.planning_requests.request.status is PlanningRequestStatus.SUBMITTED


def test_cancel_planning_request_requires_expected_version_and_updates_request() -> None:
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
