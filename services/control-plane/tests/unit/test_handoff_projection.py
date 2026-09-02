from datetime import UTC, datetime
from uuid import UUID

from tests.unit.test_task_console_projections import (
    IMPLEMENTER_ID,
    NOW as CONSOLE_NOW,
    _plan,
    _placements,
    _tasks,
    _worker_items,
)
from tests.unit.test_worker_workspace_projection import _service

from sidera_control_plane.domain.task_console_projections import (
    TaskConsoleProjectionService,
)
from sidera_control_plane.domain.worker_item_projections import (
    ActiveHandoffProjection,
    TraceDescriptorProjection,
)

WORKSPACE_ID = UUID("22000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("32000000-0000-0000-0000-000000000001")
SOURCE_EXECUTION_ID = UUID("52000000-0000-0000-0000-000000000001")
SOURCE_SESSION_ID = UUID("62000000-0000-0000-0000-000000000001")
TARGET_WORKER_ID = UUID("72000000-0000-0000-0000-000000000002")
TARGET_EXECUTION_ID = UUID("52000000-0000-0000-0000-000000000003")
TARGET_SESSION_ID = UUID("62000000-0000-0000-0000-000000000003")
TRANSITION_ID = UUID("a2000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("a2000000-0000-0000-0000-000000000002")


def _handoff(
    *,
    status: str = "running",
    attention_required: bool = False,
) -> ActiveHandoffProjection:
    return ActiveHandoffProjection(
        transition_id=TRANSITION_ID,
        proposal_id=PROPOSAL_ID,
        status=status,
        phase="transfer_workspace",
        writer_state="writer_none",
        source_execution_id=SOURCE_EXECUTION_ID,
        source_session_id=SOURCE_SESSION_ID,
        source_worker_id=UUID("72000000-0000-0000-0000-000000000001"),
        target_worker_id=TARGET_WORKER_ID,
        target_execution_id=TARGET_EXECUTION_ID,
        target_session_id=TARGET_SESSION_ID,
        attention_required=attention_required,
        failure_code="adapter_rejected" if attention_required else None,
        blocked_operation_id=None,
        available_recovery_strategies=("retry_current_phase",),
        source_trace_descriptor=TraceDescriptorProjection(
            root_task_id=ROOT_TASK_ID,
            execution_id=SOURCE_EXECUTION_ID,
            session_id=SOURCE_SESSION_ID,
            route=(
                f"/api/v1/tasks/{ROOT_TASK_ID}/executions/"
                f"{SOURCE_EXECUTION_ID}/sessions/{SOURCE_SESSION_ID}/trace"
            ),
            raw_ref=None,
        ),
        target_trace_descriptor=TraceDescriptorProjection(
            root_task_id=ROOT_TASK_ID,
            execution_id=TARGET_EXECUTION_ID,
            session_id=TARGET_SESSION_ID,
            route=(
                f"/api/v1/tasks/{ROOT_TASK_ID}/executions/"
                f"{TARGET_EXECUTION_ID}/sessions/{TARGET_SESSION_ID}/trace"
            ),
            raw_ref=None,
        ),
        started_at=datetime(2026, 9, 2, 12, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, 12, 2, tzinfo=UTC),
    )


def test_worker_workspace_exposes_handoff_without_changing_worker_facts() -> None:
    workspace = _service(active_handoff=_handoff()).compose_workspace()

    assert workspace.active_handoff is not None
    assert workspace.active_handoff.source_trace_descriptor is not None
    assert workspace.active_handoff.target_trace_descriptor is not None
    assert workspace.current_placement.subtask_key == "callback-retry"
    assert workspace.current_runtime is not None


def test_task_console_aggregates_handoff_by_subtask_and_points_attention_to_worker_item() -> None:
    graph = TaskConsoleProjectionService().compose(
        _plan(),
        tasks=_tasks(),
        worker_items=_worker_items(),
        placements=_placements(),
        executions=(),
        evidence=(),
        activity=(),
        last_change_cursor=9,
        as_of=CONSOLE_NOW,
        handoffs={
            IMPLEMENTER_ID: _handoff(),
            UUID("40000000-0000-0000-0000-000000000002"): _handoff(
                status="blocked_unknown",
                attention_required=True,
            ),
        },
    )

    node = next(item for item in graph.nodes if item.subtask_key == "callback-idempotency")
    assert node.handoff_running_count == 1
    assert node.handoff_blocked_count == 1
    assert node.attention_worker_item_count == 1
    assert node.handoff_worker_item_ids == (
        IMPLEMENTER_ID,
        UUID("40000000-0000-0000-0000-000000000002"),
    )
    assert len(graph.overview.attention_items) == 1
    assert graph.overview.attention_items[0].resource_id == UUID(
        "40000000-0000-0000-0000-000000000002"
    )
