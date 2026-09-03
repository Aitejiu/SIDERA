from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from sidera_control_plane.domain.collaboration_projections import (
    WorkerItemCollaborationSummaryProjection,
)
from sidera_control_plane.domain.worker_item_projections import (
    ArtifactSummaryProjection,
    CheckpointSummaryProjection,
    EvidenceSummaryProjection,
    ExecutionFact,
    InputFact,
    InvalidActivityCursor,
    LeaseFact,
    SessionFact,
    TraceDescriptorProjection,
    UsageSummaryProjection,
    WorkerActivityFact,
    WorkerEventFact,
    WorkerFact,
    WorkerInterventionContextProjection,
    WorkerItemFact,
    WorkerItemPlacementFact,
    WorkerItemProjectionService,
)

WORKSPACE_ID = UUID("22000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("32000000-0000-0000-0000-000000000001")
CALLBACK_TASK_ID = UUID("32000000-0000-0000-0000-000000000002")
RETRY_TASK_ID = UUID("32000000-0000-0000-0000-000000000003")
WORKER_ITEM_ID = UUID("42000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("72000000-0000-0000-0000-000000000001")
EXECUTION_ONE_ID = UUID("52000000-0000-0000-0000-000000000001")
EXECUTION_TWO_ID = UUID("52000000-0000-0000-0000-000000000002")
SESSION_ONE_ID = UUID("62000000-0000-0000-0000-000000000001")
SESSION_TWO_ID = UUID("62000000-0000-0000-0000-000000000002")
EVENT_ID = UUID("82000000-0000-0000-0000-000000000001")
ARTIFACT_ID = UUID("82000000-0000-0000-0000-000000000002")
EVIDENCE_ID = UUID("82000000-0000-0000-0000-000000000003")
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _worker_item(**changes: object) -> WorkerItemFact:
    return WorkerItemFact(
        id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        stable_key="callback-implementer",
        title="回调实现",
        responsibility="实现回调幂等逻辑",
        status="active",
        context_version=4,
        acknowledged_context_version=4,
        version=7,
        updated_at=NOW + timedelta(minutes=8),
        **changes,
    )


def _placements() -> tuple[WorkerItemPlacementFact, ...]:
    return (
        WorkerItemPlacementFact(
            plan_snapshot_id=UUID("12000000-0000-0000-0000-000000000002"),
            plan_generation=2,
            worker_item_id=WORKER_ITEM_ID,
            worker_item_key="callback-implementer",
            subtask_key="callback-retry",
            subtask_task_id=RETRY_TASK_ID,
            role="implementer",
            capability_requirements=("python",),
        ),
        WorkerItemPlacementFact(
            plan_snapshot_id=UUID("12000000-0000-0000-0000-000000000001"),
            plan_generation=1,
            worker_item_id=WORKER_ITEM_ID,
            worker_item_key="callback-implementer",
            subtask_key="callback-idempotency",
            subtask_task_id=CALLBACK_TASK_ID,
            role="implementer",
            capability_requirements=("python",),
        ),
    )


def _executions() -> tuple[ExecutionFact, ...]:
    return (
        ExecutionFact(
            id=EXECUTION_ONE_ID,
            task_id=CALLBACK_TASK_ID,
            worker_item_id=WORKER_ITEM_ID,
            lineage_id=EXECUTION_ONE_ID,
            attempt=1,
            status="succeeded",
            failure_code=None,
            assigned_worker_id=WORKER_ID,
            pinned_worker_id=None,
            active_lease_id=None,
            created_at=NOW,
            updated_at=NOW + timedelta(minutes=1),
            started_at=NOW,
            finished_at=NOW + timedelta(minutes=1),
        ),
        ExecutionFact(
            id=EXECUTION_TWO_ID,
            task_id=RETRY_TASK_ID,
            worker_item_id=WORKER_ITEM_ID,
            lineage_id=EXECUTION_ONE_ID,
            attempt=2,
            status="running",
            failure_code=None,
            assigned_worker_id=WORKER_ID,
            pinned_worker_id=None,
            active_lease_id=None,
            created_at=NOW + timedelta(minutes=4),
            updated_at=NOW + timedelta(minutes=8),
            started_at=NOW + timedelta(minutes=4),
            finished_at=None,
        ),
    )


def _sessions() -> tuple[SessionFact, ...]:
    return (
        SessionFact(
            id=SESSION_ONE_ID,
            execution_id=EXECUTION_ONE_ID,
            worker_id=WORKER_ID,
            adapter_session_id="session-old",
            status="closed",
            input_mode="queued",
            version=3,
            created_at=NOW,
            last_activity_at=NOW + timedelta(minutes=1),
        ),
        SessionFact(
            id=SESSION_TWO_ID,
            execution_id=EXECUTION_TWO_ID,
            worker_id=WORKER_ID,
            adapter_session_id="session-current",
            status="active",
            input_mode="queued",
            version=4,
            created_at=NOW + timedelta(minutes=4),
            last_activity_at=NOW + timedelta(minutes=8),
        ),
    )


def _events() -> tuple[WorkerEventFact, ...]:
    return (
        WorkerEventFact(
            id=EVENT_ID,
            execution_id=EXECUTION_TWO_ID,
            session_id=SESSION_TWO_ID,
            sequence=3,
            event_type="tool.call",
            payload={
                "tool": "pytest",
                "summary": "运行回调测试",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            occurred_at=NOW + timedelta(minutes=7),
            raw_ref="trace://tool/1",
        ),
        WorkerEventFact(
            id=UUID("82000000-0000-0000-0000-000000000004"),
            execution_id=EXECUTION_TWO_ID,
            session_id=SESSION_TWO_ID,
            sequence=4,
            event_type="checkpoint.created",
            payload={
                "title": "测试完成",
                "artifact_ids": [str(ARTIFACT_ID)],
            },
            occurred_at=NOW + timedelta(minutes=8),
            raw_ref=None,
        ),
    )


def _service(**changes: object) -> WorkerItemProjectionService:
    values: dict[str, Any] = {
        "worker_item": _worker_item(),
        "placements": _placements(),
        "executions": _executions(),
        "sessions": _sessions(),
        "workers": (
            WorkerFact(
                id=WORKER_ID,
                display_name="OpenCode callback worker",
                adapter_type="opencode",
            ),
        ),
        "bindings": (),
        "leases": (
            LeaseFact(
                id=UUID("92000000-0000-0000-0000-000000000001"),
                execution_id=EXECUTION_TWO_ID,
                worker_id=WORKER_ID,
                status="active",
                fencing_token=7,
                updated_at=NOW + timedelta(minutes=8),
            ),
        ),
        "events": _events(),
        "inputs": (
            InputFact(
                id=UUID("a2000000-0000-0000-0000-000000000001"),
                execution_id=EXECUTION_TWO_ID,
                session_id=SESSION_TWO_ID,
                text="只修改这个回调重试规则",
                status="delivered",
                operation_id=None,
                created_at=NOW + timedelta(minutes=6),
                updated_at=NOW + timedelta(minutes=6),
            ),
        ),
        "artifacts": (
            ArtifactSummaryProjection(
                artifact_id=ARTIFACT_ID,
                task_id=RETRY_TASK_ID,
                execution_id=EXECUTION_TWO_ID,
                artifact_type="test-report",
                media_type="text/plain",
                size_bytes=128,
                content_available=True,
                created_at=NOW + timedelta(minutes=8),
            ),
        ),
        "evidence": (
            EvidenceSummaryProjection(
                evidence_id=EVIDENCE_ID,
                task_id=RETRY_TASK_ID,
                execution_id=EXECUTION_TWO_ID,
                acceptance_ref="acceptance:callback",
                conclusion="回调测试通过",
                artifact_ids=(ARTIFACT_ID,),
                created_at=NOW + timedelta(minutes=8),
            ),
        ),
        "last_change_cursor": 31,
        "as_of": NOW + timedelta(minutes=8),
    }
    values.update(changes)
    return WorkerItemProjectionService(**values)


def test_worker_workspace_exposes_current_runtime_and_placement_history() -> None:
    workspace = _service().compose_workspace()

    assert workspace.worker_item.id == WORKER_ITEM_ID
    assert workspace.current_placement.subtask_key == "callback-retry"
    assert [item.plan_generation for item in workspace.placement_history] == [2, 1]
    assert workspace.current_runtime is not None
    assert workspace.current_runtime.execution_id == EXECUTION_TWO_ID
    assert workspace.current_runtime.session_id == SESSION_TWO_ID
    assert workspace.current_runtime.worker_id == WORKER_ID
    assert workspace.current_runtime.worker_display_name == "OpenCode callback worker"
    assert workspace.can_intervene
    assert workspace.read_only_reason is None
    assert workspace.intervention_context == WorkerInterventionContextProjection(
        worker_item_id=WORKER_ITEM_ID,
        plan_generation=2,
        context_version=4,
        execution_id=EXECUTION_TWO_ID,
        session_id=SESSION_TWO_ID,
        session_version=4,
        worker_id=WORKER_ID,
    )
    assert workspace.latest_checkpoint == CheckpointSummaryProjection(
        execution_id=EXECUTION_TWO_ID,
        sequence=4,
        title="测试完成",
        artifact_ids=(ARTIFACT_ID,),
        occurred_at=NOW + timedelta(minutes=8),
    )
    assert workspace.usage == UsageSummaryProjection(
        input_tokens=10,
        output_tokens=5,
        cached_tokens=0,
        cost_microunits=0,
    )


def test_worker_workspace_keeps_governance_summary_next_to_work_surface() -> None:
    collaboration = WorkerItemCollaborationSummaryProjection(
        enabled=True,
        state="waiting_release",
        waiting_summary="等待协同放行",
        upstream_links=(),
        governance_mode="paused",
        active_hold_id=UUID("a2000000-0000-0000-0000-000000000010"),
        hold_resource_version=2,
        hold_reason="用户需要先核对回调证据",
        release_state="proposed",
        available_actions=("approve", "reject", "resume_automatic"),
        proposal_summary="建议放行回调实现",
    )

    workspace = _service(collaboration=collaboration).compose_workspace()

    assert workspace.collaboration == collaboration
    assert workspace.collaboration is not None
    assert workspace.collaboration.governance_mode == "paused"
    assert workspace.collaboration.available_actions == (
        "approve",
        "reject",
        "resume_automatic",
    )
    assert workspace.collaboration.hold_reason == "用户需要先核对回调证据"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda item: replace(item, status="retired"), "worker_item_retired"),
        (lambda item: replace(item, status="paused"), "worker_item_paused"),
        (
            lambda item: replace(item, acknowledged_context_version=3),
            "context_not_acknowledged",
        ),
    ],
)
def test_worker_workspace_reports_read_only_reason(change, reason: str) -> None:
    service = _service(worker_item=change(_worker_item()))

    workspace = service.compose_workspace()

    assert not workspace.can_intervene
    assert workspace.read_only_reason == reason


def test_worker_workspace_blocks_stale_projection_and_unsupported_input() -> None:
    stale = _service(projection_stale=True).compose_workspace()
    unsupported = _service(
        sessions=(replace(_sessions()[1], input_mode="unsupported"),)
    ).compose_workspace()

    assert stale.read_only_reason == "projection_stale"
    assert unsupported.read_only_reason == "adapter_input_unsupported"


def test_worker_workspace_activity_uses_stable_cursor_and_hides_tool_body() -> None:
    activity = (
        WorkerActivityFact(
            id=UUID("b2000000-0000-0000-0000-000000000001"),
            source_type="execution_input",
            source_id=UUID("a2000000-0000-0000-0000-000000000001"),
            execution_id=EXECUTION_TWO_ID,
            session_id=SESSION_TWO_ID,
            occurred_at=NOW + timedelta(minutes=6),
            kind="user_input",
            title="用户介入",
            summary="只修改这个回调重试规则",
            correlation_id=UUID("c2000000-0000-0000-0000-000000000001"),
            source_priority=30,
            body="只修改这个回调重试规则",
            delivery_status="delivered",
            trace_descriptor=None,
        ),
        WorkerActivityFact(
            id=UUID("b2000000-0000-0000-0000-000000000002"),
            source_type="execution_event",
            source_id=EVENT_ID,
            execution_id=EXECUTION_TWO_ID,
            session_id=SESSION_TWO_ID,
            occurred_at=NOW + timedelta(minutes=7),
            kind="tool",
            title="pytest",
            summary="运行回调测试",
            correlation_id=UUID("c2000000-0000-0000-0000-000000000002"),
            source_priority=40,
            body=None,
            delivery_status=None,
            trace_descriptor=TraceDescriptorProjection(
                root_task_id=ROOT_TASK_ID,
                execution_id=EXECUTION_TWO_ID,
                session_id=SESSION_TWO_ID,
                route="/api/v1/tasks/trace",
                raw_ref="trace://tool/1",
            ),
        ),
        WorkerActivityFact(
            id=UUID("b2000000-0000-0000-0000-000000000003"),
            source_type="artifact",
            source_id=ARTIFACT_ID,
            execution_id=EXECUTION_TWO_ID,
            session_id=SESSION_TWO_ID,
            occurred_at=NOW + timedelta(minutes=8),
            kind="artifact",
            title="test-report",
            summary="生成 test-report",
            correlation_id=UUID("c2000000-0000-0000-0000-000000000003"),
            source_priority=50,
            body=None,
            delivery_status=None,
            trace_descriptor=None,
        ),
        WorkerActivityFact(
            id=UUID("b2000000-0000-0000-0000-000000000004"),
            source_type="evidence",
            source_id=EVIDENCE_ID,
            execution_id=EXECUTION_TWO_ID,
            session_id=SESSION_TWO_ID,
            occurred_at=NOW + timedelta(minutes=9),
            kind="evidence",
            title="acceptance:callback",
            summary="回调测试通过",
            correlation_id=UUID("c2000000-0000-0000-0000-000000000004"),
            source_priority=60,
            body=None,
            delivery_status=None,
            trace_descriptor=None,
        ),
    )
    service = WorkerItemProjectionService(
        worker_item=_worker_item(),
        placements=(),
        executions=(),
        sessions=(),
        workers=(),
        bindings=(),
        leases=(),
        events=(),
        inputs=(),
        artifacts=(),
        evidence=(),
        last_change_cursor=31,
        as_of=NOW,
    )

    first = service.compose_activity(activity, limit=2)
    second = service.compose_activity(
        activity,
        limit=2,
        after=first.next_cursor,
    )

    assert [item.activity_id for item in first.items + second.items] == [
        item.id for item in activity
    ]
    assert first.items[0].body == "只修改这个回调重试规则"
    assert first.items[0].delivery_status == "delivered"
    assert first.items[1].body is None
    assert first.items[1].trace_descriptor is not None
    assert first.next_cursor is not None

    with pytest.raises(InvalidActivityCursor):
        service.compose_activity(activity, limit=2, after="bad-cursor")
