from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.application.scheduling_blocks import (
    SchedulingBlockService,
    aggregate_runtime_issues,
)
from sidera_control_plane.application.worker_runtime import RuntimeCandidateInspection
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.scheduling import (
    SchedulingBlockReason,
    SchedulingIssue,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import EventModel, SchedulingBlockModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _make_execution(workspace_id: UUID, *, pinned_worker_id: UUID | None = None) -> Execution:
    execution_id = uuid4()
    return Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=uuid4(),
        lineage_id=execution_id,
        attempt=1,
        max_attempts=2,
        purpose="record scheduling diagnostics",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=pinned_worker_id,
        not_before=NOW,
    )


def _persist_execution(database: Database, workspace_id: UUID) -> Execution:
    execution = _make_execution(workspace_id)
    task = Task.create(
        task_id=execution.task_id,
        workspace_id=workspace_id,
        title="Scheduling diagnostics",
        description="Exercise scheduling block episodes",
        acceptance_criteria=("waiting facts are durable",),
        priority=50,
        requested_by="integration",
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        uow.flush()
        uow.executions.add(execution)
    return execution


def _envelope(execution: Execution, suffix: str) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=execution.workspace_id,
        command_type="ScheduleExecution",
        target_type="execution",
        actor_type="system",
        actor_id="scheduler",
        target_id=execution.id,
        payload={"execution_id": str(execution.id)},
        expected_version=execution.version,
        idempotency_key=f"schedule-{execution.id}-{suffix}",
        correlation_id=uuid4(),
    )


def _stage_command(uow: SqlAlchemyUnitOfWork, envelope: CommandEnvelope) -> None:
    uow.commands.record_applied(
        envelope,
        request_hash=f"hash-{envelope.id}",
        result=CommandResult(
            command_id=envelope.id,
            aggregate_id=envelope.target_id or uuid4(),
            aggregate_version=envelope.expected_version or 1,
            event_ids=(),
        ),
    )


def _issue(
    reason: SchedulingBlockReason,
    *,
    worker_id: UUID | None = None,
    details: dict[str, object] | None = None,
) -> SchedulingIssue:
    reason_details = dict(details or {})
    if worker_id is not None:
        reason_details["worker_id"] = str(worker_id)
    return SchedulingIssue(
        reason_code=reason,
        reason_details=reason_details,
        requires_operator=True,
    )


def _worker(workspace_id: UUID, worker_id: UUID | None = None) -> Worker:
    return Worker.register(
        worker_id=worker_id or uuid4(),
        workspace_id=workspace_id,
        adapter_type="opencode",
        display_name="diagnostic worker",
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=100,
        startup_latency_ms=100,
        now=NOW,
    )


def test_scheduling_block_service_deduplicates_an_episode_and_emits_transitions(
    database: Database,
    workspace_id: UUID,
) -> None:
    execution = _persist_execution(database, workspace_id)
    service = SchedulingBlockService()
    first_issue = _issue(
        SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING,
        details={"worker_count": 2},
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        envelope = _envelope(execution, "first")
        _stage_command(uow, envelope)
        events = service.record(
            uow,
            execution=execution,
            issue=first_issue,
            envelope=envelope,
            now=NOW,
        )
        for event in events:
            uow.events.append_with_outbox(event)

    with SqlAlchemyUnitOfWork(database) as uow:
        active = uow.scheduling_blocks.get_active_for_execution(
            execution.id, workspace_id
        )
        assert active is not None
        first_block_id = active.id
        assert active.version == 1
        assert active.occurrence_count == 1

    with SqlAlchemyUnitOfWork(database) as uow:
        envelope = _envelope(execution, "same")
        _stage_command(uow, envelope)
        events = service.record(
            uow,
            execution=execution,
            issue=first_issue,
            envelope=envelope,
            now=NOW.replace(second=1),
        )
        assert events == ()

    with SqlAlchemyUnitOfWork(database) as uow:
        active = uow.scheduling_blocks.get_active_for_execution(
            execution.id, workspace_id
        )
        assert active is not None
        assert active.id == first_block_id
        assert active.version == 2
        assert active.occurrence_count == 2

    changed_issue = _issue(
        SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE,
        worker_id=uuid4(),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        envelope = _envelope(execution, "changed")
        _stage_command(uow, envelope)
        events = service.record(
            uow,
            execution=execution,
            issue=changed_issue,
            envelope=envelope,
            now=NOW.replace(second=2),
        )
        assert [event.event_type for event in events] == [
            "ExecutionSchedulingUnblocked",
            "ExecutionSchedulingBlocked",
        ]
        for event in events:
            uow.events.append_with_outbox(event)

    with SqlAlchemyUnitOfWork(database) as uow:
        active = uow.scheduling_blocks.get_active_for_execution(
            execution.id, workspace_id
        )
        assert active is not None
        assert active.id != first_block_id
        assert active.version == 1
        assert active.reason_code is SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE

    with database.session() as session:
        blocks = session.scalars(
            select(SchedulingBlockModel).order_by(SchedulingBlockModel.created_at)
        ).all()
        events = session.scalars(
            select(EventModel).where(
                EventModel.aggregate_type == "scheduling_block"
            )
        ).all()
    assert len(blocks) == 2
    assert {block.status for block in blocks} == {"active", "resolved"}
    assert [event.event_type for event in events].count("ExecutionSchedulingBlocked") == 2
    assert [event.event_type for event in events].count("ExecutionSchedulingUnblocked") == 1
    assert all(event.aggregate_id != execution.id for event in events)


def test_resolve_closes_active_episode_and_is_noop_without_one(
    database: Database,
    workspace_id: UUID,
) -> None:
    execution = _persist_execution(database, workspace_id)
    service = SchedulingBlockService()
    issue = _issue(SchedulingBlockReason.NO_ELIGIBLE_WORKER)

    with SqlAlchemyUnitOfWork(database) as uow:
        envelope = _envelope(execution, "open")
        _stage_command(uow, envelope)
        for event in service.record(
            uow,
            execution=execution,
            issue=issue,
            envelope=envelope,
            now=NOW,
        ):
            uow.events.append_with_outbox(event)

    with SqlAlchemyUnitOfWork(database) as uow:
        envelope = _envelope(execution, "resolve")
        _stage_command(uow, envelope)
        events = service.resolve(
            uow,
            execution=execution,
            envelope=envelope,
            now=NOW.replace(second=3),
        )
        assert [event.event_type for event in events] == [
            "ExecutionSchedulingUnblocked"
        ]
        for event in events:
            uow.events.append_with_outbox(event)

    with SqlAlchemyUnitOfWork(database) as uow:
        assert (
            uow.scheduling_blocks.get_active_for_execution(execution.id, workspace_id)
            is None
        )
        envelope = _envelope(execution, "resolve-again")
        _stage_command(uow, envelope)
        assert (
            service.resolve(
                uow,
                execution=execution,
                envelope=envelope,
                now=NOW.replace(second=4),
            )
            == ()
        )

    with database.session() as session:
        event_types = session.scalars(select(EventModel.event_type)).all()
    assert event_types == [
        "ExecutionSchedulingBlocked",
        "ExecutionSchedulingUnblocked",
    ]


def test_aggregate_runtime_issues_keeps_multi_candidate_details_safe(
    workspace_id: UUID,
) -> None:
    execution = _make_execution(workspace_id)
    first = _worker(workspace_id)
    second = _worker(workspace_id)
    inspections = (
        RuntimeCandidateInspection(
            worker=first,
            resolved=None,
            issue=_issue(
                SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING,
                worker_id=first.id,
            ),
        ),
        RuntimeCandidateInspection(
            worker=second,
            resolved=None,
            issue=_issue(
                SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
                worker_id=second.id,
                details={"expected": 2, "actual": 1},
            ),
        ),
    )

    issue = aggregate_runtime_issues(
        execution=execution,
        inspections=inspections,
        base_candidates=(first, second),
    )

    assert issue.reason_code is SchedulingBlockReason.NO_ELIGIBLE_WORKER
    assert issue.reason_details == {
        "candidate_count": 2,
        "candidate_issue_counts": {
            "runtime_configuration_invalid": 1,
            "runtime_configuration_missing": 1,
        },
        "candidate_worker_ids": tuple(sorted([str(first.id), str(second.id)])),
    }
    assert "config.command" not in str(issue.reason_details)
    assert "secret_refs" not in str(issue.reason_details)


def test_aggregate_runtime_issues_prefers_pinned_worker_reason(
    workspace_id: UUID,
) -> None:
    pinned = _worker(workspace_id)
    other = _worker(workspace_id)
    execution = _make_execution(workspace_id, pinned_worker_id=pinned.id)
    pinned_issue = _issue(
        SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
        worker_id=pinned.id,
        details={"field": "adapter_config_version", "expected": 3, "actual": 2},
    )

    issue = aggregate_runtime_issues(
        execution=execution,
        inspections=(
            RuntimeCandidateInspection(worker=pinned, resolved=None, issue=pinned_issue),
            RuntimeCandidateInspection(
                worker=other,
                resolved=None,
                issue=_issue(
                    SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING,
                    worker_id=other.id,
                ),
            ),
        ),
        base_candidates=(pinned, other),
    )

    assert issue == pinned_issue


def test_aggregate_runtime_issues_distinguishes_empty_and_forbidden_candidates(
    workspace_id: UUID,
) -> None:
    execution = _make_execution(workspace_id)
    worker = _worker(workspace_id)

    empty = aggregate_runtime_issues(
        execution=execution,
        inspections=(),
        base_candidates=(),
    )
    assert empty == SchedulingIssue(
        reason_code=SchedulingBlockReason.NO_ELIGIBLE_WORKER,
        reason_details={"candidate_count": 0},
        requires_operator=False,
    )

    forbidden = aggregate_runtime_issues(
        execution=execution,
        inspections=(),
        base_candidates=(worker,),
        forbidden_worker_ids=(worker.id,),
    )
    assert forbidden == SchedulingIssue(
        reason_code=SchedulingBlockReason.ADAPTER_POLICY_REJECTED,
        reason_details={
            "candidate_count": 1,
            "forbidden_worker_ids": [str(worker.id)],
        },
        requires_operator=False,
    )
