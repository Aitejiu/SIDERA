from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkerItemPlacement,
)
from sidera_control_plane.application.proposal_materializer import ProposalMaterializer
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    CollaborationPolicyRequestV1,
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequest,
    ExecutionRequestDependency,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.plan_revisions import (
    RootTaskPlanHead,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.planning_proposals import (
    ActivationDependency,
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    PlanningProfileDescriptor,
    PlanningProposalPayload,
    PlanningProposalPayloadV4,
    ProposalRevision,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement as PlanDependencyEnforcement,
)
from sidera_control_plane.domain.plans import (
    DependencyKind,
    SubtaskDependency,
    WorkerItemDependency,
    WorkItemKind,
)
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("70000000-0000-0000-0000-000000000001")


class FakeTaskRepository:
    def __init__(self) -> None:
        self.added = []

    def add(self, task) -> None:
        self.added.append(task)


class FakePlanRepository:
    def __init__(self) -> None:
        self.added = []

    def add(self, snapshot) -> None:
        self.added.append(snapshot)

    def add_snapshot_and_advance_head(self, snapshot, *, expected_head):
        del expected_head
        self.add(snapshot)
        return RootTaskPlanHead(
            workspace_id=snapshot.workspace_id,
            root_task_id=snapshot.root_task_id,
            active_snapshot_id=snapshot.id,
            active_generation=snapshot.generation,
            active_snapshot_hash=plan_snapshot_content_hash(snapshot),
            version=1,
            updated_at=snapshot.published_at,
        )


class FakeWorkerItemRepository:
    def __init__(self) -> None:
        self.items = {}
        self.placements = {}

    def get(self, item_id, workspace_id, *, for_update=False):
        del for_update
        item = self.items.get(item_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return item

    def add(self, item) -> None:
        self.items[item.id] = item

    def list_for_subtask(self, plan_snapshot_id, subtask_key):
        return tuple(
            item
            for item in self.placements.values()
            if item.plan_snapshot_id == plan_snapshot_id
            and item.subtask_key == subtask_key
        )

    def add_placement(self, placement: WorkerItemPlacement) -> None:
        key = (placement.plan_snapshot_id, placement.worker_item_id)
        existing = self.placements.get(key)
        if existing is not None and existing != placement:
            raise AssertionError("placement changed")
        self.placements[key] = placement


class FakeExecutionRequestRepository:
    def __init__(self) -> None:
        self.added: list[ExecutionRequest] = []
        self.dependencies: list[ExecutionRequestDependency] = []

    def add(self, request: ExecutionRequest) -> None:
        self.added.append(request)

    def add_dependency(self, dependency: ExecutionRequestDependency) -> None:
        self.dependencies.append(dependency)


class FakeRequestRepository:
    def __init__(self) -> None:
        self.saved = []

    def save(self, request, previous_version: int) -> None:
        self.saved.append((request, previous_version))


class FakeWorkContextRepository:
    def __init__(self, request: PlanningRequest) -> None:
        initial_revision = WorkContextRevisionFactory().create(
            work_context_id=uuid4(),
            workspace_id=request.workspace_id,
            brief=request.briefs[0],
            revision_number=1,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
            actor=request.actor,
            created_at=request.created_at,
        )
        self.context = WorkContext.create(
            work_context_id=initial_revision.work_context_id,
            workspace_id=request.workspace_id,
            planning_request_id=request.id,
            first_revision=initial_revision,
            created_at=request.created_at,
        )
        self.revisions: dict[UUID, WorkContextRevision] = {
            initial_revision.revision_id: initial_revision
        }
        self.bound: list[tuple[WorkContextRevision, UUID, int]] = []

    def get_by_planning_request(
        self, planning_request_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> WorkContext | None:
        if (
            self.context.planning_request_id == planning_request_id
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

    def bind_root_task(
        self,
        context: WorkContext,
        root_task_id: UUID,
        revision: WorkContextRevision,
        *,
        expected_version: int,
    ) -> None:
        context.bind_root_task_with_revision(
            root_task_id,
            revision,
            expected_version=expected_version,
            now=NOW,
        )
        self.revisions[revision.revision_id] = revision
        self.bound.append((revision, root_task_id, expected_version))


class FakeMaterializationRepository:
    def __init__(self) -> None:
        self.added = []

    def add(self, materialization) -> None:
        self.added.append(materialization)

    def get_by_request(self, request_id, workspace_id):
        return next(
            (
                item
                for item in self.added
                if item.planning_request_id == request_id
                and item.workspace_id == workspace_id
            ),
            None,
        )


class FakeEventRepository:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class FakeUnitOfWork:
    def __init__(self, request: PlanningRequest) -> None:
        self.tasks = FakeTaskRepository()
        self.plans = FakePlanRepository()
        self.worker_items = FakeWorkerItemRepository()
        self.execution_requests = FakeExecutionRequestRepository()
        self.planning_requests = FakeRequestRepository()
        self.work_contexts = FakeWorkContextRepository(request)
        self.proposal_materializations = FakeMaterializationRepository()
        self.events = FakeEventRepository()

    def flush(self) -> None:
        return None


def capabilities() -> PlannerAdapterCapabilityProfile:
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


def collaboration_binding() -> CollaborationRuntimeBindingSnapshot:
    capability = CollaborationRuntimeCapabilityProfile(
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
    return CollaborationRuntimeBindingSnapshot(
        runtime_definition_id=uuid4(),
        runtime_instance_id=uuid4(),
        runtime_interface_version=capability.interface_version,
        runtime_config_version=3,
        runtime_capability_snapshot=capability,
    )


def profile() -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_key="default",
        version=1,
        display_name="Default planner",
        planner_module_instance_id=uuid4(),
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={},
        proposal_schema_version=2,
        allowed_capabilities=("code", "docs", "test"),
        adapter_preferences=("fake",),
        adapter_forbidden=("unsafe",),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=10_000,
            max_cost_microunits=1_000_000,
            max_runtime_seconds=600,
            max_revisions=3,
            max_nodes=20,
            max_concurrency=4,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(1,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=40,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=(),
    )


def proposal_fixture() -> tuple[PlanningProfile, PlanningRequest, ProposalRevision]:
    stored_profile = profile()
    snapshot = stored_profile.snapshot(capabilities())
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_snapshot=snapshot,
        objective="Build a planning control loop",
        constraints=("Keep the confirmation atomic",),
        acceptance_criteria=("The plan can be executed",),
        context_refs=("context:test",),
        requested_outputs=("proposal",),
        source_type="test",
        source_ref="test:proposal-materializer",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )
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
        provider_ref="fake:planner",
        structured_result_type="planning.proposal.v2",
        usage=run.usage,
        now=NOW,
    )

    subtasks = (
        ProposedSubtask(
            key="design",
            kind=WorkItemKind.CHILD,
            title="Design",
            goal_summary="Define the contract.",
            acceptance_refs=("acceptance:design",),
            expected_outputs=("contract",),
            milestone_key=None,
            source_refs=("brief:1",),
        ),
        ProposedSubtask(
            key="implementation",
            kind=WorkItemKind.CHILD,
            title="Implementation",
            goal_summary="Implement the change.",
            acceptance_refs=("acceptance:implementation",),
            expected_outputs=("code",),
            milestone_key=None,
            source_refs=("brief:1",),
        ),
        ProposedSubtask(
            key="verification",
            kind=WorkItemKind.CHILD,
            title="Verification",
            goal_summary="Verify the result.",
            acceptance_refs=("acceptance:verification",),
            expected_outputs=("report",),
            milestone_key=None,
            source_refs=("brief:1",),
        ),
    )
    workers = (
        ProposedWorkerItem(
            key="design-worker",
            subtask_key="design",
            title="Design worker",
            role="architect",
            responsibility="Define the contract.",
            capability_requirements=("docs",),
            worker_policy_ref="policy:default",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:1",),
        ),
        ProposedWorkerItem(
            key="backend-worker",
            subtask_key="implementation",
            title="Backend worker",
            role="backend",
            responsibility="Implement the backend.",
            capability_requirements=("code",),
            worker_policy_ref="policy:default",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:1",),
        ),
        ProposedWorkerItem(
            key="web-worker",
            subtask_key="implementation",
            title="Web worker",
            role="frontend",
            responsibility="Implement the web surface.",
            capability_requirements=("code",),
            worker_policy_ref="policy:default",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:1",),
        ),
        ProposedWorkerItem(
            key="verify-worker",
            subtask_key="verification",
            title="Verification worker",
            role="qa",
            responsibility="Verify the result.",
            capability_requirements=("test",),
            worker_policy_ref="policy:default",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:1",),
        ),
    )
    worker_dependencies = (
        WorkerItemDependency(
            upstream_worker_item_key="backend-worker",
            downstream_worker_item_key="web-worker",
            kind="hard",
            required_artifact_refs=("artifact_type:report",),
        ),
        WorkerItemDependency(
            upstream_worker_item_key="web-worker",
            downstream_worker_item_key="backend-worker",
            kind="informational",
            required_artifact_refs=(),
        ),
    )
    intents = tuple(
        ExecutionIntent(
            worker_item_key=item.key,
            purpose=item.responsibility,
            context_refs=("context:brief",),
            expected_artifact_refs=("artifact_type:diff",),
            required_evidence_refs=(f"acceptance:{item.subtask_key}",),
            capability_requirements=item.capability_requirements,
            adapter_preferences=("fake",),
            budget_class="standard",
            deadline_seconds=60,
            not_before_seconds=None,
            max_attempts=1,
            activation_dependencies=(
                ActivationDependency(
                    upstream_worker_item_key="design-worker",
                    downstream_worker_item_key=item.key,
                    kind="informational",
                    required_artifact_refs=(),
                ),
            )
            if item.key != "design-worker"
            else (),
        )
        for item in workers
    )
    payload = PlanningProposalPayload(
        schema_version=2,
        kind=PlanningRequestKind.CREATE,
        root_task=ProposedRootTask(
            title="Planning control loop",
            goal_summary="Materialize a complete executable plan.",
            acceptance_refs=("acceptance:design",),
        ),
        subtasks=subtasks,
        worker_items=workers,
        subtask_milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="design",
                downstream_subtask_key="implementation",
                kind=DependencyKind.HARD,
                required_evidence_refs=("acceptance:design",),
                enforcement=PlanDependencyEnforcement.CONTROL_PLANE,
            ),
            SubtaskDependency(
                upstream_subtask_key="design",
                downstream_subtask_key="verification",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=PlanDependencyEnforcement.CONTROL_PLANE,
            ),
            SubtaskDependency(
                upstream_subtask_key="implementation",
                downstream_subtask_key="verification",
                kind=DependencyKind.HARD,
                required_evidence_refs=("acceptance:implementation",),
                enforcement=PlanDependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=worker_dependencies,
        execution_intents=intents,
        decision_summary=DecisionSummary(
            understanding="Materialize a complete executable plan.",
            assumptions=(),
            risks=(),
            estimated_cost_microunits=10,
            estimated_runtime_seconds=60,
            estimated_concurrency=2,
            scope_includes=("control plane",),
            scope_excludes=(),
            metadata={},
            estimated_tokens=100,
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="block",
        ),
        base_descriptor=None,
    )
    proposal = ProposalRevision.create(
        planning_request_id=request.id,
        revision=1,
        brief_revision=1,
        planning_run_id=run.id,
        kind=PlanningRequestKind.CREATE,
        payload=payload,
        profile_descriptor=PlanningProfileDescriptor.from_snapshot(snapshot),
        profile_snapshot=snapshot,
        created_at=NOW,
    )
    request.mark_proposal_ready(
        run_id=run.id,
        proposal_revision=proposal.revision,
        content_hash=proposal.content_hash,
        now=NOW,
    )
    return stored_profile, request, proposal


def v4_proposal_fixture(
    *, overrides: tuple[dict[str, object], ...] = ()
) -> tuple[PlanningProfile, PlanningRequest, ProposalRevision]:
    stored_profile, legacy_request, legacy_proposal = proposal_fixture()
    stored_profile = replace(
        stored_profile,
        proposal_schema_version=4,
        collaboration_runtime_binding=collaboration_binding(),
    )
    profile_snapshot = stored_profile.snapshot(
        replace(capabilities(), proposal_schema_versions=(4,))
    )
    legacy = legacy_proposal.payload
    assert isinstance(legacy, PlanningProposalPayload)
    payload = PlanningProposalPayloadV4(
        schema_version=4,
        kind=legacy.kind,
        root_task=legacy.root_task,
        subtasks=legacy.subtasks,
        worker_items=legacy.worker_items,
        subtask_milestones=legacy.subtask_milestones,
        subtask_dependencies=legacy.subtask_dependencies,
        worker_item_dependencies=legacy.worker_item_dependencies,
        execution_intents=legacy.execution_intents,
        decision_summary=legacy.decision_summary,
        impact_policy=legacy.impact_policy,
        collaboration_policy_request=CollaborationPolicyRequestV1.from_mapping(
            {
                "mode": "enabled",
                "default_policy": {"strategy_id": "all_required"},
                "subtask_overrides": list(overrides),
            }
        ),
    )
    proposal = ProposalRevision.create(
        planning_request_id=legacy_request.id,
        revision=1,
        brief_revision=legacy_proposal.brief_revision,
        planning_run_id=legacy_proposal.planning_run_id,
        kind=legacy_proposal.kind,
        payload=payload,
        profile_descriptor=PlanningProfileDescriptor.from_snapshot(profile_snapshot),
        profile_snapshot=profile_snapshot,
        created_at=NOW,
    )
    request = replace(
        legacy_request,
        profile_snapshot=profile_snapshot,
        current_proposal_revision=proposal.revision,
        current_proposal_content_hash=proposal.content_hash,
    )
    return stored_profile, request, proposal


def envelope(request: PlanningRequest) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=request.workspace_id,
        command_type="ConfirmPlanningProposal",
        target_type="planning_request",
        actor_type="local",
        actor_id="operator",
        target_id=request.id,
        payload={
            "proposal_revision": 1,
            "proposal_content_hash": "placeholder",
            "profile_id": str(request.planning_profile_id),
            "profile_version": request.planning_profile_version,
        },
        expected_version=request.version,
        idempotency_key=uuid4().hex,
        correlation_id=uuid4(),
    )


def test_materializes_tasks_plan_worker_items_requests_and_mapping() -> None:
    stored_profile, request, proposal = proposal_fixture()
    command = envelope(request)
    command.payload["proposal_content_hash"] = proposal.content_hash
    uow = FakeUnitOfWork(request)

    result = ProposalMaterializer().materialize(
        cast(ControlPlaneUnitOfWork, uow),
        request=request,
        proposal=proposal,
        profile=stored_profile,
        envelope=command,
        now=NOW,
    )

    assert request.status is PlanningRequestStatus.MATERIALIZED
    assert len(uow.tasks.added) == 4
    assert uow.tasks.added[0].title == proposal.payload.root_task.title
    assert len(uow.plans.added) == 1
    assert uow.plans.added[0].schema_version == 2
    assert uow.plans.added[0].generation == 1
    assert uow.plans.added[0].parent_snapshot_id is None
    assert len(uow.worker_items.items) == 4
    assert len(uow.worker_items.placements) == 4
    assert len(uow.execution_requests.added) == 4
    assert all(
        item.adapter_preferences == ("fake",)
        and item.adapter_forbidden == frozenset({"unsafe"})
        for item in uow.execution_requests.added
    )
    assert len(uow.proposal_materializations.added) == 1
    assert result.materialization.root_task_id == uow.tasks.added[0].id
    assert set(result.materialization.subtask_task_mapping) == {
        "design",
        "implementation",
        "verification",
    }
    assert set(result.materialization.worker_item_mapping) == {
        "design-worker",
        "backend-worker",
        "web-worker",
        "verify-worker",
    }
    assert set(result.materialization.execution_request_mapping) == set(
        result.materialization.worker_item_mapping
    )
    assert all(
        request_item.status is ExecutionRequestStatus.BLOCKED
        for request_item in uow.execution_requests.added
        if request_item.worker_item_key != "design-worker"
    )
    assert (
        next(
            item
            for item in uow.execution_requests.added
            if item.worker_item_key == "design-worker"
        ).status
        is ExecutionRequestStatus.READY
    )
    assert uow.execution_requests.dependencies
    assert all(
        dependency.downstream_request_id != dependency.upstream_request_id
        for dependency in uow.execution_requests.dependencies
    )
    assert result.materialization.deep_link == (
        f"/workspaces/{WORKSPACE_ID}/tasks/{result.materialization.root_task_id}"
    )
    assert uow.planning_requests.saved[0][1] == 4
    assert uow.work_contexts.context.root_task_id == result.materialization.root_task_id
    assert uow.work_contexts.context.current_revision_number == 2
    context_revision = uow.work_contexts.revisions[
        uow.work_contexts.context.current_revision_id
    ]
    assert context_revision.reason is WorkContextRevisionReason.PROPOSAL_CONFIRMED
    assert context_revision.proposal_revision_id == proposal.id
    assert context_revision.plan_snapshot_id == result.materialization.plan_snapshot_id
    assert any(
        item.key == "proposal:understanding"
        for item in context_revision.snapshot.accepted_decisions
    )
    assert [event.event_type for event in uow.events.events[-2:]] == [
        "WorkContextRevised",
        "WorkContextBoundToTask",
    ]


def test_v4_materialization_resolves_policy_and_classifies_requests() -> None:
    stored_profile, request, proposal = v4_proposal_fixture(
        overrides=(
            {
                "subtask_key": "design",
                "enabled": False,
                "strategy_id": "all_required",
            },
        )
    )
    command = envelope(request)
    command.payload["proposal_content_hash"] = proposal.content_hash
    uow = FakeUnitOfWork(request)

    result = ProposalMaterializer().materialize(
        cast(ControlPlaneUnitOfWork, uow),
        request=request,
        proposal=proposal,
        profile=stored_profile,
        envelope=command,
        now=NOW,
    )

    assert result.snapshot is not None
    assert result.snapshot.schema_version == 3
    assert result.snapshot.collaboration_policy is not None
    assert (
        result.snapshot.collaboration_policy.effective_policy("design").enabled is False
    )
    requests = {item.worker_item_key: item for item in uow.execution_requests.added}
    assert (
        requests["design-worker"].collaboration_gate_mode
        is CollaborationGateMode.UNMANAGED
    )
    assert (
        requests["backend-worker"].collaboration_gate_mode
        is CollaborationGateMode.RELEASE_REQUIRED
    )
    assert (
        requests["web-worker"].collaboration_gate_mode
        is CollaborationGateMode.RELEASE_REQUIRED
    )
    assert requests["backend-worker"].status is ExecutionRequestStatus.BLOCKED
    assert requests["web-worker"].status is ExecutionRequestStatus.BLOCKED


def test_release_required_initial_gate_waits_for_run_when_static_facts_are_ready() -> None:
    stored_profile, request, proposal = v4_proposal_fixture()
    command = envelope(request)
    command.payload["proposal_content_hash"] = proposal.content_hash
    uow = FakeUnitOfWork(request)

    ProposalMaterializer().materialize(
        cast(ControlPlaneUnitOfWork, uow),
        request=request,
        proposal=proposal,
        profile=stored_profile,
        envelope=command,
        now=NOW,
    )
    release_required = next(
        item
        for item in uow.execution_requests.added
        if item.worker_item_key == "backend-worker"
    )

    ProposalMaterializer._evaluate_initial_gates(
        (release_required,), (), now=NOW
    )

    assert release_required.status is ExecutionRequestStatus.BLOCKED
    assert release_required.gate_reason == "collaboration_run_pending"


def test_dependency_expansion_deduplicates_edges_and_preserves_informational_edges() -> (
    None
):
    stored_profile, request, proposal = proposal_fixture()
    command = envelope(request)
    command.payload["proposal_content_hash"] = proposal.content_hash
    uow = FakeUnitOfWork(request)

    ProposalMaterializer().materialize(
        cast(ControlPlaneUnitOfWork, uow),
        request=request,
        proposal=proposal,
        profile=stored_profile,
        envelope=command,
        now=NOW,
    )

    edges = {
        (item.downstream_request_id, item.upstream_request_id): item
        for item in uow.execution_requests.dependencies
    }
    assert len(edges) == len(uow.execution_requests.dependencies)
    requests_by_worker = {
        item.worker_item_key: item.id for item in uow.execution_requests.added
    }
    verify = requests_by_worker["verify-worker"]
    design = requests_by_worker["design-worker"]
    assert edges[(verify, design)].enforcement is DependencyEnforcement.HARD
    assert any(
        item.enforcement is DependencyEnforcement.INFORMATIONAL
        for item in uow.execution_requests.dependencies
    )
    assert (
        sum(
            item.downstream_request_id == verify and item.upstream_request_id == design
            for item in uow.execution_requests.dependencies
        )
        == 1
    )


def test_materializer_does_not_create_executions_or_call_scheduler() -> None:
    stored_profile, request, proposal = proposal_fixture()
    command = envelope(request)
    command.payload["proposal_content_hash"] = proposal.content_hash
    uow = FakeUnitOfWork(request)

    result = ProposalMaterializer().materialize(
        cast(ControlPlaneUnitOfWork, uow),
        request=request,
        proposal=proposal,
        profile=stored_profile,
        envelope=command,
        now=NOW,
    )

    assert result.execution_ids == ()
    assert not hasattr(uow, "executions")
