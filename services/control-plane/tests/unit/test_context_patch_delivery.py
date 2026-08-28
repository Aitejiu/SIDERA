from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace, TracebackType
from typing import Self
from uuid import UUID, uuid4

from sidera_control_plane.application.context_patch_delivery import (
    ContextPatchDeliveryService,
)
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.context_patches import ContextPatchDeliveryResult
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionStatus,
)
from sidera_control_plane.domain.gateway import context_patch_operation_id
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    ExecutionBinding,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import (
    PlanTransition,
    PlanTransitionStatus,
    WorkerTransition,
    WorkerTransitionStatus,
    WriterState,
)
from sidera_control_plane.domain.plans import (
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.worker_item import WorkerItem

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("40000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("50000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("60000000-0000-0000-0000-000000000001")
LEASE_ID = UUID("70000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("80000000-0000-0000-0000-000000000001")
ADAPTER_MODULE_ID = UUID("90000000-0000-0000-0000-000000000001")
WORKSPACE_MODULE_ID = UUID("a0000000-0000-0000-0000-000000000001")
CONTEXT_REVISION_ID = UUID("b0000000-0000-0000-0000-000000000001")


class PatchRepository:
    def __init__(self) -> None:
        self.patches: dict[UUID, object] = {}
        self.acknowledgements: dict[UUID, object] = {}

    def add(self, patch: object) -> None:
        self.patches[patch.id] = deepcopy(patch)

    def get(self, patch_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        del for_update
        patch = self.patches.get(patch_id)
        return deepcopy(patch) if patch is not None and patch.workspace_id == workspace_id else None

    def get_acknowledgement(self, patch_id: UUID, workspace_id: UUID):
        acknowledgement = self.acknowledgements.get(patch_id)
        return (
            deepcopy(acknowledgement)
            if acknowledgement is not None
            and self.patches[patch_id].workspace_id == workspace_id
            else None
        )

    def add_acknowledgement(self, acknowledgement: object, workspace_id: UUID) -> None:
        patch = self.patches[acknowledgement.patch_id]
        assert patch.workspace_id == workspace_id
        existing = self.acknowledgements.get(acknowledgement.patch_id)
        if existing is not None:
            assert existing == acknowledgement
            return
        self.acknowledgements[acknowledgement.patch_id] = deepcopy(acknowledgement)


class TransitionRepository:
    def __init__(self, transition: WorkerTransition) -> None:
        self.item = deepcopy(transition)

    def get(self, transition_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        del for_update
        if self.item.id != transition_id or self.item.workspace_id != workspace_id:
            return None
        return deepcopy(self.item)

    def save(self, transition: WorkerTransition, previous_version: int) -> None:
        assert self.item.version == previous_version
        self.item = deepcopy(transition)


class WorkerItemRepository:
    def __init__(self, item: WorkerItem) -> None:
        self.item = deepcopy(item)

    def get(self, item_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        del for_update
        if self.item.id != item_id or self.item.workspace_id != workspace_id:
            return None
        return deepcopy(self.item)

    def save(self, item: WorkerItem, previous_version: int) -> None:
        assert self.item.version == previous_version
        self.item = deepcopy(item)


class LeaseRepository:
    def __init__(self, lease: AssignmentLease) -> None:
        self.lease = lease

    def get(self, lease_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        del for_update
        if self.lease.id != lease_id or self.lease.workspace_id != workspace_id:
            return None
        return deepcopy(self.lease)


class PackageRepository:
    def __init__(self, package: ContextPackageManifestV1, ack: ContextAcknowledgement) -> None:
        self.package = package
        self.ack = ack

    def get_for_consumer(self, purpose, consumer_id, workspace_id, *, for_update=False):
        del for_update
        if (
            self.package.purpose == purpose
            and self.package.consumer_id == consumer_id
            and self.package.workspace_id == workspace_id
        ):
            return self.package
        return None

    def get_ack(self, package_id, consumer_type, consumer_id, workspace_id, *, for_update=False):
        del for_update
        if (
            self.ack.package_id == package_id
            and self.ack.consumer_type == consumer_type
            and self.ack.consumer_id == consumer_id
            and self.package.workspace_id == workspace_id
        ):
            return self.ack
        return None


class FakeUnitOfWork:
    def __init__(self, state: State) -> None:
        self.worker_transitions = state.transitions
        self.worker_items = state.worker_items
        self.context_patches = state.patches
        self.context_packages = state.packages
        self.leases = state.leases
        self.plan_transitions = SimpleNamespace(
            get=lambda transition_id, workspace_id: (
                deepcopy(state.plan)
                if transition_id == state.plan.id and workspace_id == WORKSPACE_ID
                else None
            )
        )
        self.plans = SimpleNamespace(
            get=lambda snapshot_id, workspace_id: deepcopy(
                state.snapshots.get(snapshot_id)
            )
            if workspace_id == WORKSPACE_ID
            else None
        )
        self.work_contexts = SimpleNamespace(
            get_revision=lambda revision_id, workspace_id, **_kwargs: (
                state.context_revision
                if revision_id == CONTEXT_REVISION_ID and workspace_id == WORKSPACE_ID
                else None
            )
        )
        self.executions = SimpleNamespace(
            get=lambda execution_id, workspace_id, **_kwargs: (
                deepcopy(state.execution)
                if execution_id == EXECUTION_ID and workspace_id == WORKSPACE_ID
                else None
            )
        )
        self.sessions = SimpleNamespace(
            get_active_for_execution=lambda execution_id, **_kwargs: (
                deepcopy(state.session)
                if execution_id == EXECUTION_ID
                else None
            ),
            get=lambda session_id, workspace_id, **_kwargs: (
                deepcopy(state.session)
                if session_id == SESSION_ID and workspace_id == WORKSPACE_ID
                else None
            )
        )
        self.bindings = SimpleNamespace(
            get_for_execution=lambda execution_id, workspace_id, **_kwargs: (
                deepcopy(state.binding)
                if execution_id == EXECUTION_ID and workspace_id == WORKSPACE_ID
                else None
            )
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class State:
    def __init__(self) -> None:
        self.worker_item = WorkerItem.create(
            item_id=WORKER_ITEM_ID,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            title="Implement worker",
            responsibility="Implement the original requirement.",
            now=NOW,
        )
        self.worker_item.advance_context(now=NOW)
        self.base_snapshot = _snapshot(1, "Implement the original requirement.", None)
        self.target_snapshot = _snapshot(
            2, "Implement the revised requirement.", self.base_snapshot.id
        )
        self.snapshots = {
            self.base_snapshot.id: self.base_snapshot,
            self.target_snapshot.id: self.target_snapshot,
        }
        self.plan = PlanTransition(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            planning_request_id=uuid4(),
            proposal_revision_id=uuid4(),
            from_snapshot_id=self.base_snapshot.id,
            from_generation=1,
            to_snapshot_id=self.target_snapshot.id,
            to_generation=2,
            status=PlanTransitionStatus.CONVERGING,
            started_at=NOW,
            completed_at=None,
            version=1,
        )
        self.transition = WorkerTransition(
            id=uuid4(),
            plan_transition_id=self.plan.id,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            worker_item_id=WORKER_ITEM_ID,
            from_subtask_id=None,
            to_subtask_id=None,
            action=WorkerTransitionAction.PATCH,
            fallback_action=None,
            target_context_version=2,
            source_execution_id=EXECUTION_ID,
            source_session_id=None,
            target_execution_id=None,
            target_session_id=None,
            writer_state=WriterState.FENCED,
            status=WorkerTransitionStatus.WAITING_SAFE_BOUNDARY,
            failure_code=None,
            lease_id=LEASE_ID,
            fencing_token=2,
            attempt_count=0,
            next_attempt_at=NOW,
            started_at=NOW,
            completed_at=None,
            version=1,
        )
        self.execution = replace(
            Execution.create(
                execution_id=EXECUTION_ID,
                workspace_id=WORKSPACE_ID,
                task_id=ROOT_TASK_ID,
                worker_item_id=WORKER_ITEM_ID,
                lineage_id=uuid4(),
                attempt=1,
                max_attempts=2,
                purpose="worker patch test",
                capability_requirements=frozenset({"code"}),
                pinned_worker_id=WORKER_ID,
                not_before=NOW,
            ),
            status=ExecutionStatus.RUNNING,
            assigned_worker_id=WORKER_ID,
            active_lease_id=LEASE_ID,
            last_fencing_token=2,
        )
        self.session = ExecutionSession.start(
            session_id=SESSION_ID,
            workspace_id=WORKSPACE_ID,
            execution_id=EXECUTION_ID,
            worker_id=WORKER_ID,
            adapter_session_id="fake-session",
            now=NOW,
        )
        self.lease = AssignmentLease.issue(
            lease_id=LEASE_ID,
            workspace_id=WORKSPACE_ID,
            execution_id=EXECUTION_ID,
            worker_id=WORKER_ID,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            fencing_token=2,
            assignment_reason={"test": "context-patch"},
        )
        self.leases = LeaseRepository(self.lease)
        capabilities = WorkerAdapterCapabilityProfile(
            input=CapabilityInput.QUEUED,
            idempotency=IdempotencyCapability.NONE,
            resume=ResumeCapability.SAME_PROCESS,
            stop=StopCapability.EXACT_EXECUTION,
            sandbox=SandboxCapability.FILESYSTEM,
            stability=StabilityLevel.STABLE,
            context_package_versions=(1,),
            context_patch_versions=(1,),
        )
        self.binding = ExecutionBinding(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            execution_id=EXECUTION_ID,
            worker_id=WORKER_ID,
            adapter_module_id=ADAPTER_MODULE_ID,
            workspace_module_id=WORKSPACE_MODULE_ID,
            adapter_config_version=1,
            workspace_config_version=1,
            capability_snapshot=capabilities,
            lease_id=LEASE_ID,
            fencing_token=2,
            version=1,
        )
        package = ContextPackageManifestV1.create(
            package_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=EXECUTION_ID,
            worker_item_id=WORKER_ITEM_ID,
            work_context_revision_id=CONTEXT_REVISION_ID,
            context_version=1,
            control_plane_fact_snapshot={"worker_item": str(WORKER_ITEM_ID)},
            knowledge_refs=(),
            artifact_refs=(),
            evidence_refs=(),
            budget=ContextBudgetV1(
                profile_id=uuid4(),
                profile_version=1,
                max_sections=10,
                max_chars=10_000,
                max_input_tokens=2_500,
            ),
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="patch-base-package",
            created_at=NOW,
        )
        self.package = package
        self.packages = PackageRepository(
            package,
            ContextAcknowledgement(
                package_id=package.package_id,
                package_hash=package.package_hash,
                consumer_type=ContextConsumerType.WORKER,
                consumer_id=EXECUTION_ID,
                context_version=1,
                adapter_module_id=ADAPTER_MODULE_ID,
                adapter_config_version=1,
                acknowledged_at=NOW,
            ),
        )
        self.context_revision = SimpleNamespace(content_hash="c" * 64)
        self.transitions = TransitionRepository(self.transition)
        self.worker_items = WorkerItemRepository(self.worker_item)
        self.patches = PatchRepository()

    def uow(self) -> FakeUnitOfWork:
        return FakeUnitOfWork(self)


class RecordingGateway:
    def __init__(self, outcome: str = "delivered") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []
        self.state: State | None = None

    def apply_context_patch(self, **kwargs: object) -> ContextPatchDeliveryResult:
        self.calls.append(kwargs)
        materialization = kwargs["materialization"]
        assert self.state is not None
        assert self.state.patches.patches
        operation_id = context_patch_operation_id(
            kwargs["worker_transition_id"],
            kwargs["action_step"],
            logical_attempt=kwargs["logical_attempt"],
        )
        if self.outcome == "delivered":
            return ContextPatchDeliveryResult(
                outcome="delivered",
                operation_id=operation_id,
                patch_hash=materialization.patch.patch_hash,
                accepted_sequence=11,
                error_code=None,
            )
        if self.outcome == "mismatch":
            return ContextPatchDeliveryResult(
                outcome="delivered",
                operation_id=operation_id,
                patch_hash="d" * 64,
                accepted_sequence=11,
                error_code=None,
            )
        return ContextPatchDeliveryResult(
            outcome=self.outcome,
            operation_id=operation_id,
            patch_hash=materialization.patch.patch_hash,
            accepted_sequence=None,
            error_code=("prompt_busy" if self.outcome == "blocked" else "transport_lost"),
        )


def _snapshot(
    generation: int, responsibility: str, parent_snapshot_id: UUID | None
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=uuid4(),
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="test",
        source_ref="test:patch",
        title="Patch test",
        goal_summary="Apply a revised worker context.",
        acceptance_refs=("acceptance:patch",),
        published_by="test",
        published_at=NOW,
        subtasks=(
            SubtaskPlanItem(
                key="subtask",
                task_id=None,
                kind=WorkItemKind.CHILD,
                title="Subtask",
                goal_summary="Apply the revised requirement.",
                acceptance_refs=("acceptance:patch",),
                milestone_key=None,
                expected_outputs=(),
                source_refs=(),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="worker",
                worker_item_id=WORKER_ITEM_ID,
                subtask_key="subtask",
                title="Worker",
                role="implementer",
                responsibility=responsibility,
                capability_requirements=("code",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=(),
            ),
        ),
    )


def _service(state: State, gateway: RecordingGateway) -> ContextPatchDeliveryService:
    gateway.state = state
    return ContextPatchDeliveryService(
        uow_factory=state.uow,
        gateway=gateway,
        clock=lambda: NOW,
    )


def test_patch_is_materialized_before_delivery_and_acknowledges_context() -> None:
    state = State()
    gateway = RecordingGateway()
    result = _service(state, gateway).deliver(
        transition_id=state.transition.id,
        workspace_id=WORKSPACE_ID,
        expected_version=state.transitions.item.version,
        lease_id=LEASE_ID,
        fencing_token=2,
    )

    assert result.outcome == "converged"
    assert len(gateway.calls) == 1
    patch = next(iter(state.patches.patches.values()))
    assert state.patches.acknowledgements[patch.id].accepted_sequence == 11
    assert state.worker_items.item.acknowledged_context_version == 2
    assert state.transitions.item.status is WorkerTransitionStatus.CONVERGED


def test_duplicate_patch_delivery_is_idempotent_after_ack() -> None:
    state = State()
    gateway = RecordingGateway()
    service = _service(state, gateway)
    first = service.deliver(
        transition_id=state.transition.id,
        workspace_id=WORKSPACE_ID,
        expected_version=state.transition.version,
        lease_id=LEASE_ID,
        fencing_token=2,
    )
    second = service.deliver(
        transition_id=state.transition.id,
        workspace_id=WORKSPACE_ID,
        expected_version=state.transitions.item.version,
        lease_id=LEASE_ID,
        fencing_token=2,
    )

    assert first.outcome == "converged"
    assert second.outcome == "converged"
    assert len(gateway.calls) == 1


def test_patch_hash_mismatch_freezes_transition_as_unknown() -> None:
    state = State()
    gateway = RecordingGateway("mismatch")
    result = _service(state, gateway).deliver(
        transition_id=state.transition.id,
        workspace_id=WORKSPACE_ID,
        expected_version=state.transition.version,
        lease_id=LEASE_ID,
        fencing_token=2,
    )

    assert result.outcome == "unknown"
    assert state.transitions.item.status is WorkerTransitionStatus.BLOCKED_UNKNOWN
    assert state.transitions.item.writer_state is WriterState.UNKNOWN
    assert state.worker_items.item.acknowledged_context_version == 1


def test_patch_safe_boundary_block_keeps_fence_for_retry_without_replay() -> None:
    state = State()
    gateway = RecordingGateway("blocked")
    result = _service(state, gateway).deliver(
        transition_id=state.transition.id,
        workspace_id=WORKSPACE_ID,
        expected_version=state.transition.version,
        lease_id=LEASE_ID,
        fencing_token=2,
    )

    assert result.outcome == "deferred"
    assert state.transitions.item.status is WorkerTransitionStatus.WAITING_SAFE_BOUNDARY
    assert state.transitions.item.writer_state is WriterState.FENCED
    assert state.transitions.item.failure_code == "prompt_busy"


def test_stale_patch_delivery_cannot_defer_a_newer_transition_version() -> None:
    state = State()
    gateway = RecordingGateway()

    result = _service(state, gateway).deliver(
        transition_id=state.transition.id,
        workspace_id=WORKSPACE_ID,
        expected_version=state.transition.version - 1,
        lease_id=LEASE_ID,
        fencing_token=2,
    )

    assert result.outcome == "deferred"
    assert state.transitions.item.status is WorkerTransitionStatus.WAITING_SAFE_BOUNDARY
    assert state.transitions.item.failure_code is None
    assert state.transitions.item.version == 1
    assert gateway.calls == []


def test_reentering_applying_patch_freezes_without_replaying_gateway_call() -> None:
    state = State()
    state.transitions.item.begin_apply(now=NOW)
    gateway = RecordingGateway()

    result = _service(state, gateway).deliver(
        transition_id=state.transition.id,
        workspace_id=WORKSPACE_ID,
        expected_version=state.transitions.item.version,
        lease_id=LEASE_ID,
        fencing_token=2,
    )

    assert result.outcome == "unknown"
    assert state.transitions.item.status is WorkerTransitionStatus.BLOCKED_UNKNOWN
    assert gateway.calls == []
