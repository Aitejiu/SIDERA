from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.collaboration_projection import (
    CollaborationFacts,
    CollaborationProjectionAssembler,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    RuntimeResolutionError,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import (
    CollaborationFactStorePort,
    ControlPlaneUnitOfWork,
    ModuleRepositoryPort,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationEvaluation,
    CollaborationEvaluationStatus,
    CollaborationRun,
    CollaborationRunStatus,
    RuntimeProjectionSnapshot,
)
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeEnvelopeError,
    RuntimeProjectionV1,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    ExecutionStatus,
)
from sidera_control_plane.domain.plan_revisions import (
    RootTaskPlanHead,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot


class CollaborationRuntimeResolverPort(Protocol):
    def resolve(self, run: CollaborationRun) -> object:
        raise NotImplementedError


FactStoreFactory = Callable[[ControlPlaneUnitOfWork], CollaborationFactStorePort]
RuntimeResolverFactory = Callable[
    [ModuleRepositoryPort], CollaborationRuntimeResolverPort
]


@dataclass(frozen=True, slots=True)
class PreparedCollaborationEvaluation:
    run: CollaborationRun
    evaluation: CollaborationEvaluation
    projection: RuntimeProjectionV1
    operation_id: UUID
    lease_owner: str
    lease_expires_at: datetime


class CollaborationReconciler:
    """Prepare one durable, leased Runtime evaluation in a short transaction."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        fact_store_factory: FactStoreFactory,
        runtime_resolver_factory: RuntimeResolverFactory,
        projection_assembler: CollaborationProjectionAssembler | None = None,
        evaluation_lease_ttl: timedelta = timedelta(minutes=5),
        recovery_limit: int = 100,
        default_workspace_id: UUID | None = None,
        default_root_task_id: UUID | None = None,
    ) -> None:
        if evaluation_lease_ttl <= timedelta(0):
            raise ValueError("evaluation_lease_ttl must be positive")
        if recovery_limit < 1:
            raise ValueError("recovery_limit must be positive")
        if default_root_task_id is not None and default_workspace_id is None:
            raise ValueError("default_root_task_id requires default_workspace_id")
        self._uow_factory = uow_factory
        self._fact_store_factory = fact_store_factory
        self._runtime_resolver_factory = runtime_resolver_factory
        self._projection_assembler = (
            projection_assembler or CollaborationProjectionAssembler()
        )
        self._evaluation_lease_ttl = evaluation_lease_ttl
        self._recovery_limit = recovery_limit
        self._default_workspace_id = default_workspace_id
        self._default_root_task_id = default_root_task_id

    def prepare_next(
        self,
        *,
        owner_id: str,
        now: datetime,
        workspace_id: UUID | None = None,
        root_task_id: UUID | None = None,
    ) -> PreparedCollaborationEvaluation | None:
        owner = owner_id.strip()
        if not owner:
            raise ValueError("owner_id is required")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        target_workspace = workspace_id or self._default_workspace_id
        target_root = root_task_id or self._default_root_task_id
        if target_root is not None and target_workspace is None:
            raise ValueError("root_task_id requires workspace_id")

        with self._uow_factory() as uow:
            uow.collaboration_evaluations.recover_expired(
                now=now,
                limit=self._recovery_limit,
                workspace_id=target_workspace,
            )
            roots = self._candidate_roots(uow, target_workspace, target_root)
            for candidate_root in roots:
                if target_workspace is None:
                    continue
                target_cursor = uow.projection_changes.latest_target_cursor(
                    target_workspace,
                    "collaboration_runtime",
                    candidate_root,
                )
                prepared = self._prepare_root(
                    uow,
                    workspace_id=target_workspace,
                    root_task_id=candidate_root,
                    fact_cursor=target_cursor,
                    owner_id=owner,
                    now=now,
                )
                if prepared is not None:
                    return prepared
        return None

    @staticmethod
    def _candidate_roots(
        uow: ControlPlaneUnitOfWork,
        workspace_id: UUID | None,
        root_task_id: UUID | None,
    ) -> tuple[UUID, ...]:
        if root_task_id is not None:
            return (root_task_id,)
        if workspace_id is None:
            return ()
        return tuple(sorted(uow.plans.list_roots_with_snapshots(workspace_id)))

    def _prepare_root(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        root_task_id: UUID,
        fact_cursor: int,
        owner_id: str,
        now: datetime,
    ) -> PreparedCollaborationEvaluation | None:
        head = uow.plans.get_head(root_task_id, workspace_id, for_update=True)
        if head is None:
            return None
        snapshot = uow.plans.get(head.active_snapshot_id, workspace_id)
        if snapshot is None:
            return None

        current = uow.collaboration_runs.get_current(
            workspace_id, root_task_id, for_update=True
        )
        if current is None:
            historical = uow.collaboration_runs.get_for_plan(
                workspace_id,
                snapshot.id,
                snapshot.generation,
            )
            if historical is not None:
                return None
        elif not _run_matches_head(current, head):
            self._supersede(uow, current, now=now, owner_id=owner_id)
            current = None

        policy = snapshot.collaboration_policy
        if policy is None:
            if current is not None:
                self._supersede(uow, current, now=now, owner_id=owner_id)
            return None

        run = current
        if (
            run is not None
            and uow.collaboration_evaluations.get_live_for_run(
                run.workspace_id, run.id, now=now
            )
            is not None
        ):
            return None
        if run is None:
            run = _new_run(snapshot, now=now)
            preflight_error = self._preflight(uow, run)
            if preflight_error is not None:
                run.block(reason=preflight_error.code, now=now)
                uow.collaboration_runs.add(run)
                self._emit_run_event(
                    uow,
                    run,
                    event_type="CollaborationRunBlocked",
                    owner_id=owner_id,
                    payload={"reason": preflight_error.code},
                )
                return None
            run.activate(now=now)
            uow.collaboration_runs.add(run)
            uow.flush()
            self._emit_run_event(
                uow,
                run,
                event_type="CollaborationRunStarted",
                owner_id=owner_id,
                payload={},
            )
        else:
            transition_event: str | None = None
            previous_version = run.resource_version
            preflight_error = self._preflight(uow, run)
            if preflight_error is not None:
                if run.status in {
                    CollaborationRunStatus.PENDING,
                    CollaborationRunStatus.ACTIVE,
                }:
                    run.block(reason=preflight_error.code, now=now)
                    uow.collaboration_runs.save(run, previous_version)
                    self._emit_run_event(
                        uow,
                        run,
                        event_type="CollaborationRunBlocked",
                        owner_id=owner_id,
                        payload={"reason": preflight_error.code},
                    )
                return None
            if run.status is CollaborationRunStatus.PENDING:
                run.activate(now=now)
                transition_event = "CollaborationRunStarted"
            elif run.status is CollaborationRunStatus.BLOCKED:
                run.recover_same_binding(now=now)
                transition_event = "CollaborationRunRecovered"
            if run.resource_version != previous_version:
                uow.collaboration_runs.save(run, previous_version)
                if transition_event is not None:
                    self._emit_run_event(
                        uow,
                        run,
                        event_type=transition_event,
                        owner_id=owner_id,
                        payload={},
                    )

        if (
            run.last_projection_snapshot_id is not None
            and fact_cursor <= run.last_evaluated_change_cursor
        ):
            return self._reuse_existing_evaluation(
                uow,
                run=run,
                owner_id=owner_id,
                now=now,
            )

        facts = self._fact_store_factory(uow).load(
            workspace_id,
            run,
            fact_cursor=fact_cursor,
        )
        projection = self._projection_assembler.assemble(
            facts,
            fact_cursor=fact_cursor,
            now=now,
        )
        if _run_is_complete(facts):
            if run.status is CollaborationRunStatus.ACTIVE:
                previous_version = run.resource_version
                run.complete(now=now)
                uow.collaboration_runs.save(run, previous_version)
                self._emit_run_event(
                    uow,
                    run,
                    event_type="CollaborationRunCompleted",
                    owner_id=owner_id,
                    payload={},
                )
            return None

        return self._prepare_evaluation(
            uow,
            run=run,
            projection=projection,
            owner_id=owner_id,
            now=now,
        )

    def _reuse_existing_evaluation(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        run: CollaborationRun,
        owner_id: str,
        now: datetime,
    ) -> PreparedCollaborationEvaluation | None:
        projection_id = run.last_projection_snapshot_id
        if projection_id is None:
            return None
        evaluation = uow.collaboration_evaluations.get_for_projection(
            run.workspace_id, projection_id
        )
        if evaluation is None or evaluation.status in {
            CollaborationEvaluationStatus.COMPLETED,
            CollaborationEvaluationStatus.BLOCKED,
            CollaborationEvaluationStatus.EVALUATING,
        }:
            return None
        if evaluation.next_attempt_at is None or evaluation.next_attempt_at > now:
            return None
        stored_projection = uow.collaboration_ledger.get_projection(
            run.workspace_id, projection_id
        )
        if stored_projection is None:
            return None
        try:
            projection = RuntimeProjectionV1.from_mapping(
                stored_projection.payload,
                created_at=stored_projection.created_at,
            )
        except RuntimeEnvelopeError as exc:
            raise RuntimeResolutionError(
                "projection_snapshot_invalid",
                "stored collaboration projection cannot be rehydrated",
            ) from exc
        if (
            projection.projection_id != stored_projection.id
            or projection.workspace_id != stored_projection.workspace_id
            or projection.run_id != stored_projection.run_id
            or projection.content_hash != stored_projection.content_hash
            or projection.fact_cursor != stored_projection.fact_cursor
        ):
            raise RuntimeResolutionError(
                "projection_snapshot_mismatch",
                "stored collaboration projection identity does not match its ledger row",
            )
        claimed = uow.collaboration_evaluations.claim_next(
            workspace_id=run.workspace_id,
            owner_id=owner_id,
            now=now,
            lease_ttl=self._evaluation_lease_ttl,
            limit=1,
        )
        claimed_evaluation = next(
            (item for item in claimed if item.id == evaluation.id),
            None,
        )
        if claimed_evaluation is None:
            return None
        return PreparedCollaborationEvaluation(
            run=run,
            evaluation=claimed_evaluation,
            projection=projection,
            operation_id=claimed_evaluation.operation_id,
            lease_owner=owner_id,
            lease_expires_at=claimed_evaluation.lease_expires_at
            or (now + self._evaluation_lease_ttl),
        )

    def _preflight(
        self,
        uow: ControlPlaneUnitOfWork,
        run: CollaborationRun,
    ) -> RuntimeResolutionError | None:
        try:
            self._runtime_resolver_factory(uow.modules).resolve(run)
        except RuntimeResolutionError as exc:
            return exc
        except DomainError as exc:
            return RuntimeResolutionError(
                "runtime_preflight_failed",
                "collaboration runtime preflight failed",
                details={"cause": exc.code},
            )
        except Exception:  # noqa: BLE001
            return RuntimeResolutionError(
                "runtime_preflight_failed",
                "collaboration runtime preflight failed",
            )
        return None

    def _prepare_evaluation(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        run: CollaborationRun,
        projection: RuntimeProjectionV1,
        owner_id: str,
        now: datetime,
    ) -> PreparedCollaborationEvaluation | None:
        snapshot = _projection_snapshot(projection)
        stored_projection = uow.collaboration_ledger.get_or_add_projection(snapshot)
        operation_id = uuid5(
            NAMESPACE_URL,
            f"sidera:collaboration-operation:{run.id}:{stored_projection.id}",
        )
        evaluation_id = uuid5(
            NAMESPACE_URL,
            f"sidera:collaboration-evaluation:{run.id}:{stored_projection.id}",
        )
        candidate = CollaborationEvaluation.create(
            evaluation_id=evaluation_id,
            workspace_id=run.workspace_id,
            run_id=run.id,
            projection_snapshot_id=stored_projection.id,
            operation_id=operation_id,
            now=now,
        )
        stored_evaluation = uow.collaboration_evaluations.get_or_add(candidate)
        if stored_evaluation.status in {
            CollaborationEvaluationStatus.COMPLETED,
            CollaborationEvaluationStatus.BLOCKED,
        }:
            return None
        claimed = uow.collaboration_evaluations.claim_next(
            workspace_id=run.workspace_id,
            owner_id=owner_id,
            now=now,
            lease_ttl=self._evaluation_lease_ttl,
            limit=1,
        )
        evaluation = next(
            (item for item in claimed if item.id == stored_evaluation.id),
            None,
        )
        if evaluation is None:
            return None

        previous_version = run.resource_version
        run.record_projection(
            projection_snapshot_id=stored_projection.id,
            fact_cursor=stored_projection.fact_cursor,
            now=now,
        )
        uow.collaboration_runs.save(run, previous_version)
        return PreparedCollaborationEvaluation(
            run=run,
            evaluation=evaluation,
            projection=projection,
            operation_id=evaluation.operation_id,
            lease_owner=owner_id,
            lease_expires_at=evaluation.lease_expires_at
            or (now + self._evaluation_lease_ttl),
        )

    def _supersede(
        self,
        uow: ControlPlaneUnitOfWork,
        run: CollaborationRun,
        *,
        now: datetime,
        owner_id: str,
    ) -> None:
        previous_version = run.resource_version
        run.supersede(now=now)
        uow.collaboration_runs.save(run, previous_version)
        self._emit_run_event(
            uow,
            run,
            event_type="CollaborationRunSuperseded",
            owner_id=owner_id,
            payload={},
        )

    @staticmethod
    def _emit_run_event(
        uow: ControlPlaneUnitOfWork,
        run: CollaborationRun,
        *,
        event_type: str,
        owner_id: str,
        payload: dict[str, Any],
    ) -> None:
        command = CommandEnvelope(
            id=uuid4(),
            workspace_id=run.workspace_id,
            command_type="collaboration.run.reconcile",
            target_type="collaboration_run",
            actor_type="system",
            actor_id=owner_id,
            target_id=run.id,
            payload={
                "run_id": str(run.id),
                "event_type": event_type,
                "status": run.status.value,
                **payload,
            },
            expected_version=run.resource_version,
            idempotency_key=(
                f"collaboration-run-event:{run.id}:{run.resource_version}:{event_type}"
            ),
            correlation_id=run.id,
        )
        event = EventData(
            id=uuid4(),
            workspace_id=run.workspace_id,
            aggregate_type="collaboration_run",
            aggregate_id=run.id,
            aggregate_version=run.resource_version,
            event_type=event_type,
            payload={
                "run_id": str(run.id),
                "root_task_id": str(run.root_task_id),
                "plan_snapshot_id": str(run.plan_snapshot_id),
                "plan_generation": run.plan_generation,
                "status": run.status.value,
                **payload,
            },
            actor_type="system",
            actor_id=owner_id,
            causation_id=command.id,
            correlation_id=command.correlation_id,
        )
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command.id,
                aggregate_id=run.id,
                aggregate_version=run.resource_version,
                event_ids=(event.id,),
            ),
        )
        uow.events.append_with_outbox(event)


def _new_run(snapshot: TaskPlanSnapshot, *, now: datetime) -> CollaborationRun:
    if snapshot.collaboration_policy is None:
        raise ValueError("collaboration policy is required")
    binding = snapshot.collaboration_policy.binding
    run_id = uuid5(
        NAMESPACE_URL,
        f"sidera:collaboration-run:{snapshot.workspace_id}:{snapshot.root_task_id}:"
        f"{snapshot.id}:{snapshot.generation}",
    )
    return CollaborationRun.create(
        run_id=run_id,
        workspace_id=snapshot.workspace_id,
        root_task_id=snapshot.root_task_id,
        plan_snapshot_id=snapshot.id,
        plan_generation=snapshot.generation,
        plan_content_hash=_snapshot_hash(snapshot),
        runtime_definition_id=binding.runtime_definition_id,
        runtime_instance_id=binding.runtime_instance_id,
        runtime_interface_version=binding.runtime_interface_version,
        runtime_config_version=binding.runtime_config_version,
        runtime_capability_snapshot=binding.runtime_capability_snapshot,
        policy_snapshot=snapshot.collaboration_policy.to_mapping(),
        now=now,
    )


def _run_matches_head(run: CollaborationRun, head: RootTaskPlanHead) -> bool:
    return (
        run.plan_snapshot_id == head.active_snapshot_id
        and run.plan_generation == head.active_generation
        and run.plan_content_hash == head.active_snapshot_hash
    )


def _projection_snapshot(projection: RuntimeProjectionV1) -> RuntimeProjectionSnapshot:
    payload = projection.to_mapping()
    return RuntimeProjectionSnapshot(
        id=projection.projection_id,
        workspace_id=projection.workspace_id,
        run_id=projection.run_id,
        schema_version=projection.schema_version,
        plan_snapshot_id=projection.plan_snapshot_id,
        plan_generation=projection.plan_generation,
        plan_content_hash=projection.plan_content_hash,
        fact_cursor=projection.fact_cursor,
        payload=payload,
        content_hash=projection.content_hash,
        size_bytes=len(projection.canonical_bytes()),
        created_at=projection.created_at,
    )


def _run_is_complete(facts: CollaborationFacts) -> bool:
    requests = tuple(
        request
        for request in facts.requests
        if request.collaboration_gate_mode == "release_required"
    )
    closed_statuses = {"materialized", "cancelled", "obsolete"}
    if any(request.status not in closed_statuses for request in requests):
        return False
    executions_by_lineage: dict[UUID, tuple[ExecutionStatus, ...]] = {}
    grouped: dict[UUID, list[ExecutionStatus]] = {}
    for execution in facts.executions:
        try:
            status = ExecutionStatus(execution.status)
        except ValueError:
            continue
        grouped.setdefault(execution.lineage_id, []).append(status)
    executions_by_lineage = {
        lineage_id: tuple(statuses) for lineage_id, statuses in grouped.items()
    }
    for request in requests:
        if request.status not in {"materialized", "obsolete"}:
            continue
        if request.execution_lineage_id is None:
            return False
        statuses = executions_by_lineage.get(request.execution_lineage_id, ())
        if not statuses or any(
            status not in TERMINAL_EXECUTION_STATUSES for status in statuses
        ):
            return False
    return True


def _snapshot_hash(snapshot: TaskPlanSnapshot) -> str:
    return plan_snapshot_content_hash(snapshot)
