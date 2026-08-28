from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
)
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.plan_revisions import (
    RootTaskPlanHead,
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
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
    WorkerItemDependency,
    WorkerItemPlanItem,
    WorkItemKind,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
UPSTREAM_WORKER_ID = UUID("40000000-0000-0000-0000-000000000001")
DOWNSTREAM_WORKER_ID = UUID("40000000-0000-0000-0000-000000000002")


class FakeWorkerTransitionRepository:
    def __init__(self, items: tuple[WorkerTransition, ...], *, reverse: bool = False) -> None:
        self.items = {item.id: deepcopy(item) for item in items}
        self.reverse = reverse
        self.saved_ids: list[UUID] = []
        self.claimed_ids: list[UUID] = []

    def claim_pending(self, *, now: datetime, limit: int) -> tuple[WorkerTransition, ...]:
        candidates = [
            item
            for item in self.items.values()
            if item.status is WorkerTransitionStatus.PENDING
            and (item.next_attempt_at is None or item.next_attempt_at <= now)
        ]
        candidates.sort(key=lambda item: str(item.id), reverse=self.reverse)
        return tuple(deepcopy(item) for item in candidates[:limit])

    def get(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkerTransition | None:
        del for_update
        item = self.items.get(transition_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return deepcopy(item)

    def save(self, transition: WorkerTransition, previous_version: int) -> None:
        current = self.items[transition.id]
        assert current.version == previous_version
        self.items[transition.id] = deepcopy(transition)
        self.saved_ids.append(transition.id)
        if transition.status is WorkerTransitionStatus.WAITING_SAFE_BOUNDARY:
            self.claimed_ids.append(transition.id)

    def list_for_plan_transition(
        self, plan_transition_id: UUID, workspace_id: UUID
    ) -> tuple[WorkerTransition, ...]:
        return tuple(
            deepcopy(item)
            for item in self.items.values()
            if item.plan_transition_id == plan_transition_id
            and item.workspace_id == workspace_id
        )


class FakePlanTransitionRepository:
    def __init__(self, items: tuple[PlanTransition, ...]) -> None:
        self.items = {item.id: deepcopy(item) for item in items}

    def claim_converging(
        self, *, now: datetime, limit: int
    ) -> tuple[PlanTransition, ...]:
        del now
        candidates = [
            item
            for item in self.items.values()
            if item.status is PlanTransitionStatus.CONVERGING
        ]
        candidates.sort(key=lambda item: (item.started_at, str(item.id)))
        return tuple(deepcopy(item) for item in candidates[:limit])

    def get(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanTransition | None:
        del for_update
        item = self.items.get(transition_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return deepcopy(item)

    def save(self, transition: PlanTransition, previous_version: int) -> None:
        current = self.items[transition.id]
        assert current.version == previous_version
        self.items[transition.id] = deepcopy(transition)


class FakePlanRepository:
    def __init__(
        self,
        snapshot: TaskPlanSnapshot,
        head: RootTaskPlanHead,
    ) -> None:
        self.snapshot = snapshot
        self.head = head

    def get(self, snapshot_id: UUID, workspace_id: UUID) -> TaskPlanSnapshot | None:
        if snapshot_id != self.snapshot.id or workspace_id != self.snapshot.workspace_id:
            return None
        return deepcopy(self.snapshot)

    def get_head(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> RootTaskPlanHead | None:
        del for_update
        if root_task_id != self.head.root_task_id or workspace_id != self.head.workspace_id:
            return None
        return deepcopy(self.head)


class FakeExecutionRepository:
    def __init__(self, executions: tuple[Execution, ...] = ()) -> None:
        self.items = {item.id: deepcopy(item) for item in executions}

    def get(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> Execution | None:
        del for_update
        item = self.items.get(execution_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return deepcopy(item)


class FakeLeaseRepository:
    def __init__(self, leases: tuple[AssignmentLease, ...] = ()) -> None:
        self.items = {item.id: deepcopy(item) for item in leases}

    def get(
        self,
        lease_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> AssignmentLease | None:
        del for_update
        item = self.items.get(lease_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return deepcopy(item)


class FakeSessionRepository:
    def __init__(self, sessions: tuple[ExecutionSession, ...] = ()) -> None:
        self.items = {item.id: deepcopy(item) for item in sessions}

    def get_active_for_execution(
        self, execution_id: UUID, *, for_update: bool = False
    ) -> ExecutionSession | None:
        del for_update
        return next(
            (
                deepcopy(item)
                for item in self.items.values()
                if item.execution_id == execution_id
                and item.status is ExecutionSessionStatus.ACTIVE
            ),
            None,
        )

    def get(
        self,
        session_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionSession | None:
        del for_update
        item = self.items.get(session_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return deepcopy(item)


class FakeState:
    def __init__(
        self,
        *,
        workers: tuple[WorkerTransition, ...],
        plan: PlanTransition,
        snapshot: TaskPlanSnapshot,
        head: RootTaskPlanHead,
        executions: tuple[Execution, ...] = (),
        leases: tuple[AssignmentLease, ...] = (),
        sessions: tuple[ExecutionSession, ...] = (),
        reverse_claim_order: bool = False,
    ) -> None:
        self.worker_transitions = FakeWorkerTransitionRepository(
            workers, reverse=reverse_claim_order
        )
        self.plan_transitions = FakePlanTransitionRepository((plan,))
        self.plans = FakePlanRepository(snapshot, head)
        self.executions = FakeExecutionRepository(executions)
        self.leases = FakeLeaseRepository(leases)
        self.sessions = FakeSessionRepository(sessions)

    def uow(self) -> FakeUnitOfWork:
        return FakeUnitOfWork(self)


class FakeUnitOfWork:
    def __init__(self, state: FakeState) -> None:
        self.worker_transitions = state.worker_transitions
        self.plan_transitions = state.plan_transitions
        self.plans = state.plans
        self.executions = state.executions
        self.leases = state.leases
        self.sessions = state.sessions

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[object] = []


def _snapshot(
    *,
    worker_ids: tuple[tuple[str, UUID], ...] = (("worker", UPSTREAM_WORKER_ID),),
    dependency: WorkerItemDependency | None = None,
) -> TaskPlanSnapshot:
    worker_plans = tuple(
        WorkerItemPlanItem(
            key=key,
            worker_item_id=worker_id,
            subtask_key="subtask",
            title=f"{key} worker",
            role="implementer",
            responsibility=f"Handle {key}.",
            capability_requirements=("code",),
            worker_policy_ref=None,
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=(),
        )
        for key, worker_id in worker_ids
    )
    return TaskPlanSnapshot.create(
        snapshot_id=uuid4(),
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=2,
        parent_snapshot_id=uuid4(),
        source_type="test",
        source_ref="test:coordinator",
        title="Coordinator test",
        goal_summary="Exercise transition coordination.",
        acceptance_refs=("coordinator:test",),
        published_by="test",
        published_at=NOW,
        subtasks=(
            SubtaskPlanItem(
                key="subtask",
                task_id=None,
                kind=WorkItemKind.CHILD,
                title="Subtask",
                goal_summary="Run coordinator tests.",
                acceptance_refs=("coordinator:test",),
                milestone_key=None,
                expected_outputs=(),
                source_refs=(),
            ),
        ),
        worker_item_plans=worker_plans,
        worker_item_dependencies=(dependency,) if dependency is not None else (),
    )


def _head(snapshot: TaskPlanSnapshot) -> RootTaskPlanHead:
    return RootTaskPlanHead(
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        active_snapshot_id=snapshot.id,
        active_generation=snapshot.generation,
        active_snapshot_hash=plan_snapshot_content_hash(snapshot),
        version=1,
        updated_at=NOW,
    )


def _plan(snapshot: TaskPlanSnapshot) -> PlanTransition:
    return PlanTransition(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        planning_request_id=uuid4(),
        proposal_revision_id=uuid4(),
        from_snapshot_id=snapshot.parent_snapshot_id or uuid4(),
        from_generation=1,
        to_snapshot_id=snapshot.id,
        to_generation=snapshot.generation,
        status=PlanTransitionStatus.CONVERGING,
        started_at=NOW,
        completed_at=None,
        version=1,
    )


def _worker(
    plan: PlanTransition,
    worker_item_id: UUID,
    *,
    action: WorkerTransitionAction = WorkerTransitionAction.PRESERVE,
    source_execution_id: UUID | None = None,
    lease_id: UUID | None = None,
    fencing_token: int = 0,
    status: WorkerTransitionStatus = WorkerTransitionStatus.PENDING,
    writer_state: WriterState = WriterState.FENCED,
) -> WorkerTransition:
    return WorkerTransition(
        id=uuid4(),
        plan_transition_id=plan.id,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        worker_item_id=worker_item_id,
        from_subtask_id=None,
        to_subtask_id=None,
        action=action,
        fallback_action=None,
        target_context_version=2,
        source_execution_id=source_execution_id,
        source_session_id=None,
        target_execution_id=None,
        target_session_id=None,
        writer_state=writer_state,
        status=status,
        failure_code=None,
        lease_id=lease_id,
        fencing_token=fencing_token,
        attempt_count=0,
        next_attempt_at=NOW,
        started_at=None,
        completed_at=NOW if status is WorkerTransitionStatus.CONVERGED else None,
        version=1,
    )


def _coordinator(state: FakeState, gateway: RecordingGateway | None = None) -> PlanTransitionCoordinator:
    return PlanTransitionCoordinator(
        uow_factory=state.uow,
        clock=lambda: NOW,
        owner_id="test-coordinator",
        lease_ttl_seconds=30,
        gateway=gateway,
    )


def test_two_coordinators_claim_a_transition_only_once() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    worker = _worker(plan, UPSTREAM_WORKER_ID)
    state = FakeState(workers=(worker,), plan=plan, snapshot=snapshot, head=_head(snapshot))
    gateway = RecordingGateway()

    first = _coordinator(state, gateway).run_once(limit=10)
    second = _coordinator(state, gateway).run_once(limit=10)

    assert first.claimed == 1
    assert first.converged == 1
    assert second.claimed == 0
    assert second.converged == 0
    assert state.worker_transitions.items[worker.id].status is WorkerTransitionStatus.CONVERGED
    assert gateway.calls == []


def test_expired_source_lease_is_deferred_without_claiming() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    execution_id = uuid4()
    lease = AssignmentLease.issue(
        lease_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        execution_id=execution_id,
        worker_id=uuid4(),
        issued_at=NOW - timedelta(seconds=10),
        expires_at=NOW - timedelta(seconds=1),
        fencing_token=1,
        assignment_reason={"test": "expired"},
    )
    execution = replace(
        Execution.create(
            execution_id=execution_id,
            workspace_id=WORKSPACE_ID,
            task_id=ROOT_TASK_ID,
            worker_item_id=UPSTREAM_WORKER_ID,
            lineage_id=uuid4(),
            attempt=1,
            max_attempts=1,
            purpose="transition test",
            capability_requirements=frozenset({"code"}),
            pinned_worker_id=None,
            not_before=NOW,
        ),
        status=ExecutionStatus.RUNNING,
        assigned_worker_id=lease.worker_id,
        active_lease_id=lease.id,
        last_fencing_token=lease.fencing_token,
    )
    session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        execution_id=execution_id,
        worker_id=lease.worker_id,
        adapter_session_id="expired-session",
        now=NOW - timedelta(seconds=5),
    )
    worker = _worker(
        plan,
        UPSTREAM_WORKER_ID,
        action=WorkerTransitionAction.MOVE,
        source_execution_id=execution_id,
    )
    state = FakeState(
        workers=(worker,),
        plan=plan,
        snapshot=snapshot,
        head=_head(snapshot),
        executions=(execution,),
        leases=(lease,),
        sessions=(session,),
    )

    result = _coordinator(state).run_once(limit=10)

    assert result.claimed == 0
    assert result.deferred == 1
    assert state.worker_transitions.items[worker.id].status is WorkerTransitionStatus.PENDING


def test_old_transition_fence_cannot_be_taken_by_another_coordinator() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    worker = _worker(
        plan,
        UPSTREAM_WORKER_ID,
        lease_id=uuid4(),
        fencing_token=2,
    )
    state = FakeState(workers=(worker,), plan=plan, snapshot=snapshot, head=_head(snapshot))

    result = _coordinator(state).run_once(limit=10)

    assert result.claimed == 0
    assert result.deferred == 1
    assert state.worker_transitions.items[worker.id].status is WorkerTransitionStatus.PENDING


def test_changed_plan_head_does_not_continue_old_transition() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    worker = _worker(plan, UPSTREAM_WORKER_ID)
    changed_head = replace(_head(snapshot), active_snapshot_id=uuid4(), active_generation=3)
    state = FakeState(
        workers=(worker,), plan=plan, snapshot=snapshot, head=changed_head
    )
    gateway = RecordingGateway()

    result = _coordinator(state, gateway).run_once(limit=10)

    assert result.claimed == 0
    assert result.deferred == 1
    assert state.worker_transitions.items[worker.id].status is WorkerTransitionStatus.PENDING
    assert gateway.calls == []


def test_already_converged_preserve_transition_is_not_claimed() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    worker = _worker(
        plan,
        UPSTREAM_WORKER_ID,
        status=WorkerTransitionStatus.CONVERGED,
        writer_state=WriterState.CLOSED,
    )
    state = FakeState(workers=(worker,), plan=plan, snapshot=snapshot, head=_head(snapshot))

    result = _coordinator(state).run_once(limit=10)

    assert result.claimed == 0
    assert result.converged == 0
    assert result.deferred == 0


def test_move_converges_without_touching_sessions_or_gateway() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    worker = _worker(plan, UPSTREAM_WORKER_ID, action=WorkerTransitionAction.MOVE)
    state = FakeState(workers=(worker,), plan=plan, snapshot=snapshot, head=_head(snapshot))
    sessions_before = deepcopy(state.sessions.items)
    gateway = RecordingGateway()

    result = _coordinator(state, gateway).run_once(limit=10)

    assert result.claimed == 1
    assert result.converged == 1
    assert state.sessions.items == sessions_before
    assert gateway.calls == []


def test_dependency_order_is_stable_even_when_claim_query_is_reversed() -> None:
    dependency = WorkerItemDependency(
        upstream_worker_item_key="upstream",
        downstream_worker_item_key="downstream",
        kind="dependency",
        required_artifact_refs=(),
    )
    snapshot = _snapshot(
        worker_ids=(
            ("upstream", UPSTREAM_WORKER_ID),
            ("downstream", DOWNSTREAM_WORKER_ID),
        ),
        dependency=dependency,
    )
    plan = _plan(snapshot)
    upstream = _worker(plan, UPSTREAM_WORKER_ID, action=WorkerTransitionAction.MOVE)
    downstream = _worker(plan, DOWNSTREAM_WORKER_ID, action=WorkerTransitionAction.MOVE)
    state = FakeState(
        workers=(downstream, upstream),
        plan=plan,
        snapshot=snapshot,
        head=_head(snapshot),
        reverse_claim_order=True,
    )
    coordinator = _coordinator(state)

    first = coordinator.run_once(limit=10)
    second = coordinator.run_once(limit=10)

    assert first.claimed == 1
    assert second.claimed == 1
    assert state.worker_transitions.claimed_ids == [upstream.id, downstream.id]
