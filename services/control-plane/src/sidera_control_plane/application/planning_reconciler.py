from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_requests import (
    build_planning_context_assembly_request,
)
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestStatus,
    PlanningRun,
    PlanningRunLease,
    PlanningRunLeaseStatus,
    PlanningRunStatus,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    UnknownOutcomePolicy,
)


@dataclass(frozen=True, slots=True)
class PlanningReconcileResult:
    expired: int
    retried: int
    blocked: int
    failed: int


class PlanningReconciler:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        owner_id: str,
        clock: Callable[[], datetime] | None = None,
        planning_context_ttl_seconds: int | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if (
            planning_context_ttl_seconds is not None
            and planning_context_ttl_seconds < 1
        ):
            raise ValueError("planning_context_ttl_seconds must be positive")
        self._uow_factory = uow_factory
        self._owner_id = owner_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._planning_context_ttl_seconds = planning_context_ttl_seconds

    def run_once(self, limit: int = 50) -> PlanningReconcileResult:
        if limit <= 0:
            raise ValueError("planning reconciler limit must be positive")
        now = self._clock()
        expired = 0
        retried = 0
        blocked = 0
        failed = 0
        with self._uow_factory() as uow:
            expired_leases = uow.planning_leases.claim_expired(now=now, limit=limit)
            expired_run_ids = {lease.planning_run_id for lease in expired_leases}
            expired = len(expired_leases)
            for lease in expired_leases:
                outcome = self._reconcile_expired(uow, lease, now)
                retried += outcome[0]
                blocked += outcome[1]
                failed += outcome[2]

            for run in uow.planning_runs.claim_failed(now=now, limit=limit):
                if run.id in expired_run_ids:
                    continue
                outcome = self._reconcile_failed(uow, run, now)
                retried += outcome[0]
                blocked += outcome[1]
                failed += outcome[2]
        return PlanningReconcileResult(
            expired=expired,
            retried=retried,
            blocked=blocked,
            failed=failed,
        )

    def _reconcile_expired(
        self,
        uow: ControlPlaneUnitOfWork,
        lease: PlanningRunLease,
        now: datetime,
    ) -> tuple[int, int, int]:
        run = uow.planning_runs.get(
            lease.planning_run_id, lease.workspace_id, for_update=True
        )
        if run is None:
            self._expire_lease(uow, lease, now)
            return (0, 0, 0)
        request = uow.planning_requests.get(
            run.planning_request_id, lease.workspace_id, for_update=True
        )
        self._expire_lease(uow, lease, now)
        if request is None or run.status is not PlanningRunStatus.RUNNING:
            return (0, 0, 0)
        if request.status is not PlanningRequestStatus.PLANNING:
            return (0, 0, 0)
        run = request.run(run.id)
        run.lease_id = lease.id
        run.fencing_token = lease.fencing_token
        previous_request_version = request.version
        previous_run_version = run.version
        if run.profile_snapshot.execution_mode == "native_profile":
            link = uow.planning_authorities.get_link(
                run.workspace_id, run.id, for_update=True
            )
            uncertain = (
                link is not None
                and not link.retry_boundary_closed
                and link.status != "pending"
            )
            if uncertain:
                uow.planning_authorities.save_link(
                    link,
                    status="cleanup_pending" if link.run_id is not None else "unknown",
                    reason_code="native_lease_expired",
                )
            transition = request.block_run_unknown if uncertain else request.fail_run
            transition(
                run_id=run.id,
                lease_id=lease.id,
                fencing_token=lease.fencing_token,
                error_code="native_lease_expired",
                error_message="native planning lease expired without a committed result",
                now=now,
            )
            uow.planning_runs.save(run, previous_run_version)
            uow.planning_requests.save(request, previous_request_version)
            self._record_event(
                uow,
                request=request,
                event_type="PlanningRunUpdated",
                payload={
                    "planning_run_id": str(run.id),
                    "status": run.status.value,
                    "error_code": run.error_code,
                    "retry_queued": False,
                    "next_run_id": None,
                },
            )
            return (0, int(uncertain), int(not uncertain))
        request.fail_run(
            run_id=run.id,
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            error_code="lease_expired",
            error_message="planning worker lease expired before a result was committed",
            now=now,
        )
        uow.planning_runs.save(run, previous_run_version)
        should_retry = self._should_retry(run, request, now, uow=uow)
        new_run = None
        if should_retry:
            new_run = self._retry_request(uow, request, previous_request_version, now)
        else:
            uow.planning_requests.save(request, previous_request_version)
        self._record_event(
            uow,
            request=request,
            event_type="PlanningRunUpdated",
            payload={
                "planning_run_id": str(run.id),
                "status": run.status.value,
                "error_code": run.error_code,
                "retry_queued": new_run is not None,
                "next_run_id": str(new_run.id) if new_run is not None else None,
            },
        )
        if new_run is not None:
            return (1, 0, 0)
        return (0, 0, 1)

    def _reconcile_failed(
        self,
        uow: ControlPlaneUnitOfWork,
        run: PlanningRun,
        now: datetime,
    ) -> tuple[int, int, int]:
        request = uow.planning_requests.get(
            run.planning_request_id, run.workspace_id, for_update=True
        )
        if request is None or request.current_run_id != run.id:
            return (0, 0, 0)
        if request.status is not PlanningRequestStatus.FAILED:
            return (0, 0, 0)
        if (
            uow.planning_leases.get_active_for_run(
                run.id, run.workspace_id, for_update=True
            )
            is not None
        ):
            return (0, 0, 0)
        if not self._should_retry(run, request, now, uow=uow):
            return (0, 0, 1)
        previous_request_version = request.version
        new_run = self._retry_request(uow, request, previous_request_version, now)
        self._record_event(
            uow,
            request=request,
            event_type="PlanningBlockedRunRetried",
            payload={
                "failed_run_id": str(run.id),
                "planning_run_id": str(new_run.id),
                "attempt": new_run.attempt,
                "reason": run.error_code,
            },
        )
        return (1, 0, 0)

    def _retry_request(
        self,
        uow: ControlPlaneUnitOfWork,
        request: PlanningRequest,
        previous_request_version: int,
        now: datetime,
    ) -> PlanningRun:
        new_run_id = uuid4()
        request.retry_failed(new_run_id=new_run_id, now=now)
        new_run = request.runs[-1]
        uow.planning_runs.add(new_run)
        if self._planning_context_ttl_seconds is not None:
            self._add_planning_context_assembly(uow, request, new_run)
        uow.planning_requests.save(request, previous_request_version)
        return new_run

    def _add_planning_context_assembly(
        self,
        uow: ControlPlaneUnitOfWork,
        request: PlanningRequest,
        run: PlanningRun,
    ) -> None:
        ttl_seconds = self._planning_context_ttl_seconds
        if ttl_seconds is None:
            return
        context = uow.work_contexts.get_by_planning_request(
            request.id, request.workspace_id
        )
        if context is None and request.root_task_id is not None:
            context = uow.work_contexts.get_by_root_task(
                request.root_task_id, request.workspace_id
            )
        if context is None:
            raise NotFound("work context not found for planning retry")
        revision = uow.work_contexts.get_revision(
            context.current_revision_id, request.workspace_id
        )
        if revision is None:
            raise NotFound("work context revision not found for planning retry")
        uow.context_assemblies.add(
            build_planning_context_assembly_request(
                run=run,
                brief=request.briefs[-1],
                work_context_revision=revision,
                ttl_seconds=ttl_seconds,
                created_at=run.queued_at,
                planning_kind=request.kind,
                revision_base=request.base_descriptor,
            )
        )

    def _should_retry(
        self,
        run: PlanningRun,
        request: PlanningRequest,
        now: datetime,
        *,
        uow: ControlPlaneUnitOfWork,
    ) -> bool:
        policy = request.profile_snapshot.retry_policy
        retries_used = max(
            sum(
                candidate.brief_revision == run.brief_revision
                for candidate in request.runs
            )
            - 1,
            0,
        )
        if retries_used >= policy.max_retries:
            return False
        if run.error_code != "lease_expired" and run.finished_at is not None:
            backoff_index = min(retries_used, len(policy.backoff_seconds) - 1)
            retry_at = run.finished_at + timedelta(
                seconds=policy.backoff_seconds[backoff_index]
            )
            if now < retry_at:
                return False
        if run.profile_snapshot.execution_mode == "native_profile":
            link = uow.planning_authorities.get_link(run.workspace_id, run.id)
            if link is None or not link.retry_boundary_closed:
                return False
            if run.error_code in {
                "invalid_planner_output",
                "invalid_result",
                "proposal_schema_invalid",
            }:
                return policy.invalid_output_policy is InvalidOutputPolicy.RETRY
            return run.error_code in {"timeout", "rate_limited"}
        if run.error_code == "unknown_outcome":
            return (
                policy.unknown_outcome_policy is UnknownOutcomePolicy.RETRY
                and run.capability_snapshot.idempotency.value == "operation-id"
            )
        if run.error_code in {"invalid_result", "proposal_schema_invalid"}:
            return policy.invalid_output_policy is InvalidOutputPolicy.RETRY
        return run.error_code in {
            "timeout",
            "rate_limited",
            "lease_expired",
            "planning_lease_unavailable",
        }

    def _expire_lease(
        self, uow: ControlPlaneUnitOfWork, lease: PlanningRunLease, now: datetime
    ) -> None:
        previous_version = lease.version
        lease.status = PlanningRunLeaseStatus.EXPIRED
        lease.released_at = now
        lease.version += 1
        uow.planning_leases.save(lease, previous_version)

    def _record_event(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        request: PlanningRequest,
        event_type: str,
        payload: dict[str, object],
    ) -> EventData:
        envelope = CommandEnvelope(
            id=uuid4(),
            workspace_id=request.workspace_id,
            command_type="PlanningReconcilerRun",
            target_type="planning_request",
            actor_type="system",
            actor_id=self._owner_id,
            target_id=request.id,
            payload=payload,
            expected_version=None,
            idempotency_key=f"{event_type}:{request.id}:{request.version}:{uuid4()}",
            correlation_id=request.correlation_id,
        )
        event = EventData(
            id=uuid4(),
            workspace_id=request.workspace_id,
            aggregate_type="planning_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type=event_type,
            payload=payload,
            actor_type="system",
            actor_id=self._owner_id,
            causation_id=envelope.id,
            correlation_id=request.correlation_id,
        )
        uow.events.append_with_outbox(event)
        uow.commands.record_applied(
            envelope,
            hash_request(envelope),
            CommandResult(
                command_id=envelope.id,
                aggregate_id=request.id,
                aggregate_version=request.version,
                event_ids=(event.id,),
                data={"planning_run_id": payload.get("planning_run_id")},
            ),
        )
        return event
