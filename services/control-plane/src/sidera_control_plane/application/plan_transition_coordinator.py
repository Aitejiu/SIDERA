from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.errors import StaleFencingToken
from sidera_control_plane.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    ExecutionStatus,
)
from sidera_control_plane.domain.lease import LeaseStatus
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import (
    WorkerTransition,
    WorkerTransitionStatus,
)


class TransitionRuntimeGateway(Protocol):
    """Reserved runtime boundary for patch/continuation actions."""


@dataclass(frozen=True, slots=True)
class PlanTransitionRunResult:
    claimed: int
    converged: int
    deferred: int
    failed: int
    reconciled: int


@dataclass(frozen=True, slots=True)
class _ClaimedWorkerTransition:
    id: UUID
    workspace_id: UUID
    plan_transition_id: UUID
    version: int
    lease_id: UUID | None
    fencing_token: int


class PlanTransitionCoordinator:
    """Advance the non-invasive part of a confirmed plan transition.

    The coordinator deliberately handles only preserve and move in this task.
    Both actions retain the existing Execution/Session and therefore need no
    adapter call. More invasive actions are left pending for the later runtime
    stages instead of being guessed at here.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        clock: Callable[[], datetime] | None = None,
        owner_id: str = "plan-transition-coordinator",
        lease_ttl_seconds: int = 60,
        backoff_max_seconds: int = 60,
        gateway: TransitionRuntimeGateway | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if lease_ttl_seconds < 1 or backoff_max_seconds < 1:
            raise ValueError("transition coordinator limits must be positive")
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_id = owner_id.strip()
        self._lease_ttl_seconds = lease_ttl_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._gateway = gateway

    def run_once(self, limit: int = 20) -> PlanTransitionRunResult:
        if limit < 1:
            raise ValueError("transition coordinator limit must be positive")
        now = self._clock()
        claimed, deferred = self._claim_pending(now=now, limit=limit)
        converged = 0
        failed = 0
        touched_plan_ids: set[UUID] = set()
        for item in claimed:
            touched_plan_ids.add(item.plan_transition_id)
            try:
                if self._converge_preserve_or_move(item, now=self._clock()):
                    converged += 1
                else:
                    deferred += 1
            except Exception:  # noqa: BLE001
                failed += 1
        reconciled = self._reconcile_plans(
            now=self._clock(), limit=max(limit, len(touched_plan_ids))
        )
        return PlanTransitionRunResult(
            claimed=len(claimed),
            converged=converged,
            deferred=deferred,
            failed=failed,
            reconciled=reconciled,
        )

    def _claim_pending(
        self, *, now: datetime, limit: int
    ) -> tuple[tuple[_ClaimedWorkerTransition, ...], int]:
        claimed: list[_ClaimedWorkerTransition] = []
        deferred = 0
        with self._uow_factory() as uow:
            candidates = uow.worker_transitions.claim_pending(now=now, limit=limit)
            for transition in self._dependency_order(uow, candidates):
                if transition.action not in {
                    WorkerTransitionAction.PRESERVE,
                    WorkerTransitionAction.MOVE,
                }:
                    continue
                if not self._head_matches(uow, transition):
                    deferred += 1
                    continue
                if not self._dependencies_ready(uow, transition):
                    deferred += 1
                    continue
                lease = self._resolve_claim_lease(uow, transition, now=now)
                if lease is None:
                    deferred += 1
                    continue
                previous_version = transition.version
                try:
                    transition.claim(
                        lease_id=lease[0],
                        fencing_token=lease[1],
                        now=now,
                    )
                    transition.wait_for_safe_boundary(now=now)
                    uow.worker_transitions.save(transition, previous_version)
                except StaleFencingToken:
                    deferred += 1
                    continue
                claimed.append(
                    _ClaimedWorkerTransition(
                        id=transition.id,
                        workspace_id=transition.workspace_id,
                        plan_transition_id=transition.plan_transition_id,
                        version=transition.version,
                        lease_id=transition.lease_id,
                        fencing_token=transition.fencing_token,
                    )
                )
        return tuple(claimed), deferred

    def _converge_preserve_or_move(
        self, claimed: _ClaimedWorkerTransition, *, now: datetime
    ) -> bool:
        with self._uow_factory() as uow:
            transition = uow.worker_transitions.get(
                claimed.id,
                claimed.workspace_id,
                for_update=True,
            )
            if transition is None:
                return False
            if (
                transition.version != claimed.version
                or transition.status is not WorkerTransitionStatus.WAITING_SAFE_BOUNDARY
                or transition.lease_id != claimed.lease_id
                or transition.fencing_token != claimed.fencing_token
            ):
                return False
            if not self._head_matches(uow, transition):
                return False
            if not self._runtime_is_safe(uow, transition, now=now):
                return False
            previous_version = transition.version
            transition.converge(now=now)
            uow.worker_transitions.save(transition, previous_version)
            return True

    def _reconcile_plans(self, *, now: datetime, limit: int) -> int:
        reconciled = 0
        with self._uow_factory() as uow:
            plans = uow.plan_transitions.claim_converging(now=now, limit=limit)
            for plan in plans:
                workers = uow.worker_transitions.list_for_plan_transition(
                    plan.id, plan.workspace_id
                )
                previous_version = plan.version
                plan.reconcile(workers, now=now)
                uow.plan_transitions.save(plan, previous_version)
                reconciled += 1
        return reconciled

    def _resolve_claim_lease(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> tuple[UUID | None, int] | None:
        next_token = max(1, transition.fencing_token + 1)
        if transition.source_execution_id is None:
            return None, next_token

        execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
            for_update=True,
        )
        if execution is None:
            return None
        if not self._runtime_is_safe(uow, transition, now=now, execution=execution):
            return None
        if execution.active_lease_id is None:
            return (None, next_token)
        lease = uow.leases.get(
            execution.active_lease_id,
            transition.workspace_id,
            for_update=True,
        )
        if lease is None or lease.status is not LeaseStatus.ACTIVE:
            return None
        if now >= lease.expires_at:
            return None
        if transition.fencing_token > lease.fencing_token:
            return None
        return lease.id, lease.fencing_token

    def _runtime_is_safe(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
        execution: object | None = None,
    ) -> bool:
        if transition.source_execution_id is None:
            return True
        source = execution
        if source is None:
            source = uow.executions.get(
                transition.source_execution_id,
                transition.workspace_id,
                for_update=True,
            )
        if source is None:
            return False
        active_lease_id = getattr(source, "active_lease_id", None)
        has_valid_lease = False
        if active_lease_id is not None:
            lease = uow.leases.get(
                active_lease_id,
                transition.workspace_id,
                for_update=True,
            )
            if lease is None or lease.status is not LeaseStatus.ACTIVE:
                return False
            if now >= lease.expires_at:
                return False
            has_valid_lease = True
        active_session = uow.sessions.get_active_for_execution(
            transition.source_execution_id
        )
        if active_session is not None:
            return False
        if transition.source_session_id is not None:
            session = uow.sessions.get(
                transition.source_session_id,
                transition.workspace_id,
                for_update=True,
            )
            if session is not None:
                return False
        return has_valid_lease or getattr(source, "status", None) in {
            *TERMINAL_EXECUTION_STATUSES,
            ExecutionStatus.QUEUED,
        }

    @staticmethod
    def _head_matches(
        uow: ControlPlaneUnitOfWork, transition: WorkerTransition
    ) -> bool:
        plan = uow.plan_transitions.get(
            transition.plan_transition_id,
            transition.workspace_id,
        )
        if plan is None:
            return False
        head = uow.plans.get_head(
            transition.root_task_id,
            transition.workspace_id,
        )
        return bool(
            head is not None
            and head.active_snapshot_id == plan.to_snapshot_id
            and head.active_generation == plan.to_generation
        )

    @staticmethod
    def _dependencies_ready(
        uow: ControlPlaneUnitOfWork, transition: WorkerTransition
    ) -> bool:
        plan = uow.plan_transitions.get(
            transition.plan_transition_id, transition.workspace_id
        )
        if plan is None:
            return False
        snapshot = uow.plans.get(plan.to_snapshot_id, transition.workspace_id)
        if snapshot is None:
            return False
        if not snapshot.worker_item_dependencies:
            return True
        worker_key_by_id = {
            item.worker_item_id: item.key
            for item in snapshot.worker_item_plans
            if item.worker_item_id is not None
        }
        current_key = worker_key_by_id.get(transition.worker_item_id)
        if current_key is None:
            return False
        statuses = {
            item.worker_item_id: item.status
            for item in uow.worker_transitions.list_for_plan_transition(
                transition.plan_transition_id, transition.workspace_id
            )
        }
        for dependency in snapshot.worker_item_dependencies:
            if dependency.downstream_worker_item_key != current_key:
                continue
            if dependency.kind != "dependency":
                continue
            upstream_id = next(
                (
                    worker_id
                    for worker_id, key in worker_key_by_id.items()
                    if key == dependency.upstream_worker_item_key
                ),
                None,
            )
            if upstream_id is None or statuses.get(upstream_id) is not WorkerTransitionStatus.CONVERGED:
                return False
        return True

    @staticmethod
    def _dependency_order(
        uow: ControlPlaneUnitOfWork,
        transitions: Iterable[WorkerTransition],
    ) -> tuple[WorkerTransition, ...]:
        items = tuple(transitions)
        by_plan: dict[UUID, list[WorkerTransition]] = defaultdict(list)
        for item in items:
            by_plan[item.plan_transition_id].append(item)
        ordered: list[WorkerTransition] = []
        for plan_id, plan_items in sorted(by_plan.items(), key=lambda pair: str(pair[0])):
            first = plan_items[0]
            plan = uow.plan_transitions.get(plan_id, first.workspace_id)
            snapshot = (
                uow.plans.get(plan.to_snapshot_id, first.workspace_id)
                if plan is not None
                else None
            )
            key_by_id = (
                {
                    item.worker_item_id: item.key
                    for item in snapshot.worker_item_plans
                    if item.worker_item_id is not None
                }
                if snapshot is not None
                else {}
            )
            by_key = {key_by_id.get(item.worker_item_id, str(item.worker_item_id)): item for item in plan_items}
            edges: dict[UUID, set[UUID]] = {item.id: set() for item in plan_items}
            indegree = {item.id: 0 for item in plan_items}
            if snapshot is not None:
                for dependency in snapshot.worker_item_dependencies:
                    upstream = by_key.get(dependency.upstream_worker_item_key)
                    downstream = by_key.get(dependency.downstream_worker_item_key)
                    if upstream is None or downstream is None or downstream.id in edges[upstream.id]:
                        continue
                    edges[upstream.id].add(downstream.id)
                    indegree[downstream.id] += 1
            ready = sorted(
                (item.id for item in plan_items if indegree[item.id] == 0),
                key=lambda item_id: _stable_transition_key(item_id, by_key),
            )
            by_id = {item.id: item for item in plan_items}
            group_ordered: list[WorkerTransition] = []
            while ready:
                item_id = ready.pop(0)
                group_ordered.append(by_id[item_id])
                for downstream_id in sorted(edges[item_id], key=str):
                    indegree[downstream_id] -= 1
                    if indegree[downstream_id] == 0:
                        ready.append(downstream_id)
                        ready.sort(key=lambda value: _stable_transition_key(value, by_key))
            if len(group_ordered) < len(plan_items):
                seen = {item.id for item in group_ordered}
                group_ordered.extend(
                    sorted(
                        (item for item in plan_items if item.id not in seen),
                        key=lambda item: _stable_transition_key(item.id, by_key),
                    )
                )
            ordered.extend(group_ordered)
        return tuple(ordered)


def _stable_transition_key(
    transition_id: UUID, by_key: dict[str, WorkerTransition]
) -> tuple[str, str]:
    item = next((value for value in by_key.values() if value.id == transition_id), None)
    return (str(item.worker_item_id) if item is not None else "", str(transition_id))
