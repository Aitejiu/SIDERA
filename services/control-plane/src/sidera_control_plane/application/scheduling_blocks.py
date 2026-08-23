from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_runtime import (
    RuntimeCandidateInspection,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.scheduling import (
    SchedulingBlock,
    SchedulingBlockReason,
    SchedulingIssue,
)
from sidera_control_plane.domain.worker import Worker

_SAFE_DETAIL_KEYS = frozenset(
    {
        "actual",
        "adapter_type",
        "candidate_count",
        "candidate_issue_counts",
        "candidate_worker_ids",
        "expected",
        "field",
        "forbidden_worker_ids",
        "lifecycle",
        "provider",
        "reason",
        "worker_count",
        "worker_id",
    }
)


class SchedulingBlockService:
    """Persist one active scheduling-wait episode per queued execution."""

    def record(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        execution: Execution,
        issue: SchedulingIssue,
        envelope: CommandEnvelope,
        now: datetime,
    ) -> tuple[EventData, ...]:
        safe_issue = _safe_issue(issue)
        active = uow.scheduling_blocks.get_active_for_execution(
            execution.id,
            execution.workspace_id,
            for_update=True,
        )
        if active is None:
            block = SchedulingBlock.open(
                block_id=uuid4(),
                workspace_id=execution.workspace_id,
                execution_id=execution.id,
                issue=safe_issue,
                now=now,
            )
            uow.scheduling_blocks.add(block)
            return (_blocked_event(envelope, execution, block),)

        if active.fingerprint == safe_issue.fingerprint:
            previous_version = active.version
            active.observe(issue=safe_issue, now=now)
            uow.scheduling_blocks.save(active, previous_version)
            return ()

        previous_version = active.version
        active.resolve(now=now)
        uow.scheduling_blocks.save(active, previous_version)
        unblocked = _unblocked_event(envelope, execution, active)

        block = SchedulingBlock.open(
            block_id=uuid4(),
            workspace_id=execution.workspace_id,
            execution_id=execution.id,
            issue=safe_issue,
            now=now,
        )
        uow.scheduling_blocks.add(block)
        return (unblocked, _blocked_event(envelope, execution, block))

    def resolve(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        execution: Execution,
        envelope: CommandEnvelope,
        now: datetime,
    ) -> tuple[EventData, ...]:
        active = uow.scheduling_blocks.get_active_for_execution(
            execution.id,
            execution.workspace_id,
            for_update=True,
        )
        if active is None:
            return ()
        previous_version = active.version
        active.resolve(now=now)
        uow.scheduling_blocks.save(active, previous_version)
        return (_unblocked_event(envelope, execution, active),)


def aggregate_runtime_issues(
    *,
    execution: Execution,
    inspections: Collection[RuntimeCandidateInspection],
    base_candidates: Collection[Worker],
    forbidden_worker_ids: Collection[UUID] = (),
) -> SchedulingIssue:
    """Turn candidate-level runtime failures into one safe scheduling issue."""

    candidates = tuple(base_candidates)
    forbidden_ids = set(forbidden_worker_ids)
    inspection_by_worker = {inspection.worker.id: inspection for inspection in inspections}

    if execution.pinned_worker_id is not None:
        pinned_inspection = inspection_by_worker.get(execution.pinned_worker_id)
        if pinned_inspection is not None and pinned_inspection.issue is not None:
            return _safe_issue(pinned_inspection.issue)
        if execution.pinned_worker_id in forbidden_ids:
            return SchedulingIssue(
                reason_code=SchedulingBlockReason.ADAPTER_POLICY_REJECTED,
                reason_details={
                    "candidate_count": len(candidates),
                    "forbidden_worker_ids": [str(execution.pinned_worker_id)],
                },
                requires_operator=False,
            )
        return SchedulingIssue(
            reason_code=SchedulingBlockReason.PINNED_WORKER_UNAVAILABLE,
            reason_details={"worker_id": str(execution.pinned_worker_id)},
            requires_operator=True,
        )

    if not candidates:
        return SchedulingIssue(
            reason_code=SchedulingBlockReason.NO_ELIGIBLE_WORKER,
            reason_details={"candidate_count": 0},
            requires_operator=False,
        )

    candidate_ids = {candidate.id for candidate in candidates}
    forbidden_candidate_ids = sorted(
        (str(worker_id) for worker_id in candidate_ids.intersection(forbidden_ids)),
    )
    if forbidden_candidate_ids and len(forbidden_candidate_ids) == len(candidate_ids):
        return SchedulingIssue(
            reason_code=SchedulingBlockReason.ADAPTER_POLICY_REJECTED,
            reason_details={
                "candidate_count": len(candidates),
                "forbidden_worker_ids": forbidden_candidate_ids,
            },
            requires_operator=False,
        )

    issues = tuple(
        inspection.issue
        for inspection in inspections
        if inspection.issue is not None
    )
    if not issues:
        return SchedulingIssue(
            reason_code=SchedulingBlockReason.NO_ELIGIBLE_WORKER,
            reason_details={
                "candidate_count": len(candidates),
                "candidate_worker_ids": sorted(str(candidate.id) for candidate in candidates),
            },
            requires_operator=False,
        )

    issue_counts: dict[str, int] = {}
    for issue in issues:
        reason = issue.reason_code.value
        issue_counts[reason] = issue_counts.get(reason, 0) + 1
    return SchedulingIssue(
        reason_code=SchedulingBlockReason.NO_ELIGIBLE_WORKER,
        reason_details={
            "candidate_count": len(candidates),
            "candidate_issue_counts": dict(sorted(issue_counts.items())),
            "candidate_worker_ids": sorted(str(candidate.id) for candidate in candidates),
        },
        requires_operator=any(issue.requires_operator for issue in issues),
    )


def _blocked_event(
    envelope: CommandEnvelope,
    execution: Execution,
    block: SchedulingBlock,
) -> EventData:
    return _event(
        envelope,
        execution=execution,
        block=block,
        event_type="ExecutionSchedulingBlocked",
    )


def _unblocked_event(
    envelope: CommandEnvelope,
    execution: Execution,
    block: SchedulingBlock,
) -> EventData:
    return _event(
        envelope,
        execution=execution,
        block=block,
        event_type="ExecutionSchedulingUnblocked",
    )


def _event(
    envelope: CommandEnvelope,
    *,
    execution: Execution,
    block: SchedulingBlock,
    event_type: str,
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=execution.workspace_id,
        aggregate_type="scheduling_block",
        aggregate_id=block.id,
        aggregate_version=block.version,
        event_type=event_type,
        payload={
            "execution_id": str(execution.id),
            "scheduling_block_id": str(block.id),
            "status": block.status.value,
            "reason_code": block.reason_code.value,
            "reason_details": _plain_json(block.reason_details),
            "fingerprint": block.fingerprint,
            "occurrence_count": block.occurrence_count,
            "requires_operator": block.requires_operator,
        },
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )


def _safe_issue(issue: SchedulingIssue) -> SchedulingIssue:
    return SchedulingIssue(
        reason_code=issue.reason_code,
        reason_details={
            key: value
            for key, value in issue.reason_details.items()
            if key in _SAFE_DETAIL_KEYS
        },
        requires_operator=issue.requires_operator,
    )


def _plain_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value
