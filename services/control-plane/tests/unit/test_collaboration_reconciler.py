from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from uuid import UUID

from sidera_control_plane.application.collaboration_projection import (
    CollaborationExecutionFact,
    CollaborationFacts,
    CollaborationPlanHeadFact,
    CollaborationProjectionAssembler,
    CollaborationRequestFact,
)
from sidera_control_plane.application.collaboration_reconciler import (
    CollaborationReconciler,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    ResolvedCollaborationRuntime,
    RuntimeResolutionError,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationEvaluation,
    CollaborationEvaluationStatus,
    CollaborationPolicyEntry,
    CollaborationPolicySnapshotV1,
    CollaborationRun,
    CollaborationRunStatus,
    CollaborationStrategy,
    RuntimeProjectionSnapshot,
)
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)
from sidera_control_plane.domain.plan_revisions import (
    RootTaskPlanHead,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot

WORKSPACE_ID = UUID("21000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
PLAN_SNAPSHOT_ID = UUID("32000000-0000-0000-0000-000000000001")
RUN_ID = UUID("33000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("34000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("35000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("36000000-0000-0000-0000-000000000001")
DEFINITION_ID = UUID("37000000-0000-0000-0000-000000000001")
INSTANCE_ID = UUID("38000000-0000-0000-0000-000000000001")
LINEAGE_ID = UUID("39000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("3a000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _capability() -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime.v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.STABLE,
    )


def _policy() -> CollaborationPolicySnapshotV1:
    return CollaborationPolicySnapshotV1(
        schema_version=1,
        runtime_definition_id=DEFINITION_ID,
        runtime_instance_id=INSTANCE_ID,
        runtime_interface_version="collaboration-runtime.v1",
        runtime_config_version=3,
        runtime_capability_snapshot=_capability(),
        default_policy=CollaborationPolicyEntry(
            enabled=True,
            strategy_id=CollaborationStrategy.ALL_REQUIRED,
        ),
        subtask_overrides=(),
    )


def _snapshot(
    *,
    snapshot_id: UUID = PLAN_SNAPSHOT_ID,
    generation: int = 3,
    policy: CollaborationPolicySnapshotV1 | None = None,
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=3,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="reconciler-test",
        title="Reconciler plan",
        goal_summary="Prepare a collaboration evaluation",
        acceptance_refs=("acceptance:reconciler",),
        published_by="test",
        published_at=NOW,
        collaboration_policy=policy,
    )


def _head(snapshot: TaskPlanSnapshot) -> RootTaskPlanHead:
    return RootTaskPlanHead(
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        active_snapshot_id=snapshot.id,
        active_generation=snapshot.generation,
        active_snapshot_hash=plan_snapshot_content_hash(snapshot),
        version=snapshot.generation,
        updated_at=NOW,
    )


def _facts(run: CollaborationRun, snapshot: TaskPlanSnapshot) -> CollaborationFacts:
    policy = snapshot.collaboration_policy
    return CollaborationFacts(
        plan_head=CollaborationPlanHeadFact(
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            run_id=run.id,
            plan_snapshot_id=snapshot.id,
            plan_generation=snapshot.generation,
            plan_content_hash=run.plan_content_hash,
            run_resource_version=run.resource_version,
            runtime_capability_snapshot=_capability(),
            policy_snapshot=policy.to_mapping() if policy is not None else None,
            run_status=run.status.value,
        ),
        plan_snapshot=snapshot,
        requests=(
            CollaborationRequestFact(
                id=REQUEST_ID,
                subtask_key="subtask-a",
                worker_item_key="worker-a",
                subtask_task_id=SUBTASK_ID,
                worker_item_id=WORKER_ITEM_ID,
                version=1,
                status="blocked",
                collaboration_gate_mode="release_required",
                gate_reason="collaboration_release_pending",
                execution_lineage_id=None,
                first_execution_id=None,
                expected_artifact_refs=(),
                expected_evidence_refs=(),
            ),
        ),
        dependencies=(),
        executions=(),
        artifacts=(),
        evidence=(),
        releases=(),
        previous_strategy_state={},
    )


def _run(snapshot: TaskPlanSnapshot, *, run_id: UUID = RUN_ID) -> CollaborationRun:
    assert snapshot.collaboration_policy is not None
    binding = snapshot.collaboration_policy.binding
    return CollaborationRun.create(
        run_id=run_id,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        plan_snapshot_id=snapshot.id,
        plan_generation=snapshot.generation,
        plan_content_hash=plan_snapshot_content_hash(snapshot),
        runtime_definition_id=binding.runtime_definition_id,
        runtime_instance_id=binding.runtime_instance_id,
        runtime_interface_version=binding.runtime_interface_version,
        runtime_config_version=binding.runtime_config_version,
        runtime_capability_snapshot=binding.runtime_capability_snapshot,
        policy_snapshot=snapshot.collaboration_policy.to_mapping(),
        now=NOW,
    )


class FakePlanRepository:
    def __init__(self, snapshot: TaskPlanSnapshot) -> None:
        self.snapshot = snapshot
        self.head = _head(snapshot)

    def list_roots_with_snapshots(self, workspace_id: UUID) -> tuple[UUID, ...]:
        return (self.head.root_task_id,)

    def get_head(
        self, root_task_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> RootTaskPlanHead | None:
        return self.head

    def get(self, snapshot_id: UUID, workspace_id: UUID) -> TaskPlanSnapshot | None:
        return self.snapshot if snapshot_id == self.snapshot.id else None


class FakeRunRepository:
    def __init__(self, run: CollaborationRun | None = None) -> None:
        self.runs: dict[UUID, CollaborationRun] = {}
        if run is not None:
            self.runs[run.id] = run
        self.saved: list[tuple[UUID, int]] = []

    def get_current(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        for_update: bool = False,
    ) -> CollaborationRun | None:
        return next(
            (
                run
                for run in self.runs.values()
                if run.workspace_id == workspace_id
                and run.root_task_id == root_task_id
                and run.status
                in {
                    CollaborationRunStatus.PENDING,
                    CollaborationRunStatus.ACTIVE,
                    CollaborationRunStatus.BLOCKED,
                }
            ),
            None,
        )

    def get_for_plan(
        self,
        workspace_id: UUID,
        plan_snapshot_id: UUID,
        plan_generation: int,
        *,
        for_update: bool = False,
    ) -> CollaborationRun | None:
        return next(
            (
                run
                for run in self.runs.values()
                if run.workspace_id == workspace_id
                and run.plan_snapshot_id == plan_snapshot_id
                and run.plan_generation == plan_generation
            ),
            None,
        )

    def add(self, run: CollaborationRun) -> None:
        self.runs[run.id] = run

    def save(self, run: CollaborationRun, previous_version: int) -> None:
        assert run.resource_version == previous_version + 1
        self.saved.append((run.id, previous_version))
        self.runs[run.id] = run


class FakeEvaluationRepository:
    def __init__(self, evaluations: tuple[CollaborationEvaluation, ...] = ()) -> None:
        self.evaluations = {item.id: item for item in evaluations}

    def get_or_add(
        self, evaluation: CollaborationEvaluation
    ) -> CollaborationEvaluation:
        existing = next(
            (
                item
                for item in self.evaluations.values()
                if item.run_id == evaluation.run_id
                and item.projection_snapshot_id == evaluation.projection_snapshot_id
            ),
            None,
        )
        if existing is not None:
            return existing
        self.evaluations[evaluation.id] = evaluation
        return evaluation

    def get_for_projection(
        self, workspace_id: UUID, projection_snapshot_id: UUID
    ) -> CollaborationEvaluation | None:
        return next(
            (
                item
                for item in self.evaluations.values()
                if item.workspace_id == workspace_id
                and item.projection_snapshot_id == projection_snapshot_id
            ),
            None,
        )

    def get_live_for_run(
        self, workspace_id: UUID, run_id: UUID, *, now: datetime
    ) -> CollaborationEvaluation | None:
        return next(
            (
                item
                for item in self.evaluations.values()
                if item.workspace_id == workspace_id
                and item.run_id == run_id
                and item.status is CollaborationEvaluationStatus.EVALUATING
                and item.lease_expires_at is not None
                and item.lease_expires_at > now
            ),
            None,
        )

    def recover_expired(
        self,
        *,
        now: datetime,
        limit: int,
        workspace_id: UUID | None = None,
    ) -> tuple[CollaborationEvaluation, ...]:
        recovered: list[CollaborationEvaluation] = []
        for evaluation in tuple(self.evaluations.values()):
            if (
                len(recovered) >= limit
                or evaluation.status is not CollaborationEvaluationStatus.EVALUATING
                or (
                    workspace_id is not None and evaluation.workspace_id != workspace_id
                )
                or evaluation.lease_expires_at is None
                or evaluation.lease_expires_at > now
            ):
                continue
            previous = evaluation.resource_version
            evaluation.schedule_retry(
                error_code="evaluation_lease_expired",
                diagnostic_summary="evaluation lease expired",
                next_attempt_at=now,
                now=now,
            )
            assert evaluation.resource_version == previous + 1
            recovered.append(evaluation)
        return tuple(recovered)

    def claim_next(
        self,
        *,
        workspace_id: UUID | None,
        owner_id: str,
        now: datetime,
        lease_ttl: timedelta,
        limit: int,
    ) -> tuple[CollaborationEvaluation, ...]:
        claimed: list[CollaborationEvaluation] = []
        for evaluation in sorted(
            self.evaluations.values(), key=lambda item: str(item.id)
        ):
            if len(claimed) >= limit:
                break
            if workspace_id is not None and evaluation.workspace_id != workspace_id:
                continue
            if any(
                item.workspace_id == evaluation.workspace_id
                and item.run_id == evaluation.run_id
                and item.status is CollaborationEvaluationStatus.EVALUATING
                and item.lease_expires_at is not None
                and item.lease_expires_at > now
                for item in self.evaluations.values()
            ):
                continue
            if evaluation.status not in {
                CollaborationEvaluationStatus.PENDING,
                CollaborationEvaluationStatus.RETRY_WAIT,
            }:
                continue
            if evaluation.next_attempt_at is None or evaluation.next_attempt_at > now:
                continue
            previous = evaluation.resource_version
            evaluation.claim(
                owner_id=owner_id,
                lease_expires_at=now + lease_ttl,
                now=now,
            )
            assert evaluation.resource_version == previous + 1
            claimed.append(evaluation)
        return tuple(claimed)


class FakeLedgerRepository:
    def __init__(self) -> None:
        self.projections: dict[tuple[UUID, int, str], RuntimeProjectionSnapshot] = {}

    def get_or_add_projection(
        self, snapshot: RuntimeProjectionSnapshot
    ) -> RuntimeProjectionSnapshot:
        key = (snapshot.run_id, snapshot.schema_version, snapshot.content_hash)
        existing = self.projections.get(key)
        if existing is not None:
            return existing
        self.projections[key] = snapshot
        return snapshot

    def get_projection(
        self, workspace_id: UUID, projection_snapshot_id: UUID
    ) -> RuntimeProjectionSnapshot | None:
        return next(
            (
                snapshot
                for snapshot in self.projections.values()
                if snapshot.workspace_id == workspace_id
                and snapshot.id == projection_snapshot_id
            ),
            None,
        )


class FakeProjectionChanges:
    def __init__(self, order: list[str], cursor: int = 17) -> None:
        self.order = order
        self.cursor = cursor
        self.target_cursor = cursor
        self.reads: list[UUID] = []

    def latest_cursor(self, workspace_id: UUID) -> int:
        self.order.append("cursor")
        self.reads.append(workspace_id)
        return self.cursor

    def latest_target_cursor(
        self, workspace_id: UUID, projection_type: str, resource_id: UUID
    ) -> int:
        self.order.append("cursor")
        self.reads.append(workspace_id)
        return self.target_cursor


class FakeEvents:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append_with_outbox(self, event: object) -> None:
        self.events.append(event)


class FakeCommands:
    def __init__(self) -> None:
        self.commands: list[object] = []

    def record_applied(
        self,
        command: object,
        request_hash: str,
        result: object,
    ) -> None:
        self.commands.append(command)


class FakeUow:
    def __init__(self, plan: FakePlanRepository, run_repo: FakeRunRepository) -> None:
        self.plans = plan
        self.collaboration_runs = run_repo
        self.collaboration_evaluations = FakeEvaluationRepository()
        self.collaboration_ledger = FakeLedgerRepository()
        self.order: list[str] = []
        self.projection_changes = FakeProjectionChanges(self.order)
        self.events = FakeEvents()
        self.commands = FakeCommands()
        self.modules = object()
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True

    def flush(self) -> None:
        return None


class FakeFactStore:
    def __init__(self, uow: FakeUow, snapshot: TaskPlanSnapshot) -> None:
        self.uow = uow
        self.snapshot = snapshot
        self.cursors: list[int] = []
        self.override: CollaborationFacts | None = None

    def load(
        self,
        workspace_id: UUID,
        run: CollaborationRun,
        *,
        fact_cursor: int,
    ) -> CollaborationFacts:
        self.uow.order.append("facts")
        self.cursors.append(fact_cursor)
        return self.override or _facts(run, self.snapshot)


class FakeRuntimeResolver:
    def __init__(self, *, error: RuntimeResolutionError | None = None) -> None:
        self.error = error
        self.runs: list[CollaborationRun] = []

    def resolve(self, run: CollaborationRun) -> ResolvedCollaborationRuntime:
        self.runs.append(run)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            runtime=object(),
            provider="reference",
            interface_version=run.runtime_interface_version,
            config_version=run.runtime_config_version,
        )


@dataclass
class Harness:
    snapshot: TaskPlanSnapshot
    uow: FakeUow
    facts: FakeFactStore
    resolver: FakeRuntimeResolver
    reconciler: CollaborationReconciler


def _harness(
    *,
    snapshot: TaskPlanSnapshot | None = None,
    run: CollaborationRun | None = None,
    resolver: FakeRuntimeResolver | None = None,
) -> Harness:
    selected_snapshot = snapshot or _snapshot(policy=_policy())
    plan = FakePlanRepository(selected_snapshot)
    run_repo = FakeRunRepository(run)
    uow = FakeUow(plan, run_repo)
    facts = FakeFactStore(uow, selected_snapshot)
    runtime_resolver = resolver or FakeRuntimeResolver()
    reconciler = CollaborationReconciler(
        uow_factory=lambda: uow,
        fact_store_factory=lambda _: facts,
        runtime_resolver_factory=lambda _: runtime_resolver,
        projection_assembler=CollaborationProjectionAssembler(),
        evaluation_lease_ttl=timedelta(minutes=5),
    )
    return Harness(selected_snapshot, uow, facts, runtime_resolver, reconciler)


def test_plan_invalidation_creates_exact_pinned_run_and_first_evaluation() -> None:
    harness = _harness()

    prepared = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert prepared is not None
    assert prepared.run.plan_snapshot_id == PLAN_SNAPSHOT_ID
    assert prepared.run.plan_generation == 3
    assert prepared.projection.fact_cursor == 17
    assert prepared.evaluation.operation_id == prepared.operation_id
    assert prepared.evaluation.status is CollaborationEvaluationStatus.EVALUATING
    assert prepared.run.status is CollaborationRunStatus.ACTIVE
    assert harness.resolver.runs[0].runtime_instance_id == INSTANCE_ID
    assert harness.uow.committed is True
    assert harness.uow.rolled_back is False


def test_policy_null_head_creates_no_run() -> None:
    harness = _harness(snapshot=_snapshot(policy=None))

    assert (
        harness.reconciler.prepare_next(
            owner_id="worker-a",
            now=NOW,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
        )
        is None
    )
    assert harness.uow.collaboration_runs.runs == {}


def test_second_wakeup_does_not_create_a_second_live_evaluation() -> None:
    harness = _harness()

    first = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )
    second = harness.reconciler.prepare_next(
        owner_id="worker-b",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert first is not None
    assert second is None
    assert len(harness.uow.collaboration_runs.runs) == 1
    assert len(harness.uow.collaboration_evaluations.evaluations) == 1


def test_new_wakeup_does_not_enqueue_while_same_run_evaluation_is_live() -> None:
    harness = _harness()
    first = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )
    assert first is not None
    harness.uow.projection_changes.cursor = 18

    second = harness.reconciler.prepare_next(
        owner_id="worker-b",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert second is None
    assert len(harness.uow.collaboration_evaluations.evaluations) == 1
    assert harness.facts.cursors == [17]


def test_blocked_run_recovers_only_after_exact_binding_preflight() -> None:
    snapshot = _snapshot(policy=_policy())
    blocked = _run(snapshot)
    blocked.block(reason="runtime_config_mismatch", now=NOW)
    resolver = FakeRuntimeResolver()
    harness = _harness(snapshot=snapshot, run=blocked, resolver=resolver)

    prepared = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert prepared is not None
    assert prepared.run.status is CollaborationRunStatus.ACTIVE
    assert any(
        getattr(event, "event_type", None) == "CollaborationRunRecovered"
        for event in harness.uow.events.events
    )


def test_blocked_run_stays_blocked_when_exact_binding_is_unavailable() -> None:
    snapshot = _snapshot(policy=_policy())
    blocked = _run(snapshot)
    blocked.block(reason="runtime_config_mismatch", now=NOW)
    resolver = FakeRuntimeResolver(
        error=RuntimeResolutionError(
            "runtime_config_mismatch", "the exact config changed"
        )
    )
    harness = _harness(snapshot=snapshot, run=blocked, resolver=resolver)

    prepared = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert prepared is None
    assert blocked.status is CollaborationRunStatus.BLOCKED
    assert blocked.blocked_reason == "runtime_config_mismatch"


def test_expired_evaluation_is_recovered_with_the_same_operation_id() -> None:
    harness = _harness()
    first = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )
    assert first is not None
    first.evaluation.lease_expires_at = NOW - timedelta(seconds=1)

    second = harness.reconciler.prepare_next(
        owner_id="worker-b",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert second is not None
    assert second.operation_id == first.operation_id
    assert second.evaluation.attempt == 2


def test_reconciler_can_scan_workspace_roots_without_explicit_root() -> None:
    harness = _harness()

    prepared = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
    )

    assert prepared is not None


def test_new_plan_generation_supersedes_old_run_and_starts_new_generation() -> None:
    harness = _harness()
    first = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )
    assert first is not None

    next_snapshot = _snapshot(
        snapshot_id=UUID("32000000-0000-0000-0000-000000000002"),
        generation=4,
        policy=_policy(),
    )
    harness.uow.plans.snapshot = next_snapshot
    harness.uow.plans.head = _head(next_snapshot)
    harness.facts.snapshot = next_snapshot
    harness.uow.projection_changes.cursor = 18

    second = harness.reconciler.prepare_next(
        owner_id="worker-b",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert second is not None
    assert second.run.plan_snapshot_id == next_snapshot.id
    assert second.run.plan_generation == 4
    assert first.run.status is CollaborationRunStatus.SUPERSEDED
    assert len(harness.uow.collaboration_runs.runs) == 2
    assert any(
        getattr(event, "event_type", None) == "CollaborationRunSuperseded"
        for event in harness.uow.events.events
    )


def test_cursor_is_captured_before_authoritative_facts_are_loaded() -> None:
    harness = _harness()

    prepared = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert prepared is not None
    assert harness.uow.order.index("cursor") < harness.uow.order.index("facts")
    assert harness.facts.cursors == [17]


def test_cursor_capture_is_scoped_to_the_collaboration_root() -> None:
    harness = _harness()
    harness.uow.projection_changes.cursor = 99
    harness.uow.projection_changes.target_cursor = 17

    prepared = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert prepared is not None
    assert prepared.projection.fact_cursor == 17
    assert harness.facts.cursors == [17]


def test_due_retry_reuses_projection_and_operation_id() -> None:
    harness = _harness()
    first = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )
    assert first is not None
    first.evaluation.schedule_retry(
        error_code="runtime_timeout",
        diagnostic_summary="temporary timeout",
        next_attempt_at=NOW,
        now=NOW,
    )

    second = harness.reconciler.prepare_next(
        owner_id="worker-b",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert second is not None
    assert second.projection.projection_id == first.projection.projection_id
    assert second.operation_id == first.operation_id
    assert second.evaluation.attempt == 2


def test_terminal_request_and_lineage_complete_the_run() -> None:
    snapshot = _snapshot(policy=_policy())
    run = _run(snapshot)
    run.activate(now=NOW)
    harness = _harness(snapshot=snapshot, run=run)
    base = _facts(run, snapshot)
    request = replace(
        base.requests[0],
        status="materialized",
        execution_lineage_id=LINEAGE_ID,
        first_execution_id=EXECUTION_ID,
    )
    harness.facts.override = replace(
        base,
        requests=(request,),
        executions=(
            CollaborationExecutionFact(
                id=EXECUTION_ID,
                lineage_id=LINEAGE_ID,
                task_id=SUBTASK_ID,
                worker_item_id=WORKER_ITEM_ID,
                attempt=1,
                status=ExecutionStatus.SUCCEEDED.value,
                failure_code=None,
                started_at=NOW,
                finished_at=NOW,
                version=1,
            ),
        ),
    )

    prepared = harness.reconciler.prepare_next(
        owner_id="worker-a",
        now=NOW,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
    )

    assert prepared is None
    assert run.status is CollaborationRunStatus.COMPLETED
    assert any(
        getattr(event, "event_type", None) == "CollaborationRunCompleted"
        for event in harness.uow.events.events
    )
