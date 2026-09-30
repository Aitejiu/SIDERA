import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sidera_control_plane.application.worker_trace_queries import (
    RawTraceFact,
    WorkerTraceProjectionService,
    WorkerTraceQueryService,
)
from sidera_control_plane.domain.projections import TraceCategory

FIXTURE = Path(__file__).parents[1] / "fixtures" / "worker_trace" / "provider_neutral_v1.json"


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def raw_facts() -> tuple[RawTraceFact, ...]:
    payload = load_fixture()
    common = {
        key: payload[key]
        for key in (
            "schema_version",
            "workspace_id",
            "root_task_id",
            "plan_snapshot_id",
            "work_item_key",
            "execution_id",
            "session_id",
        )
    }
    return tuple(
        RawTraceFact.from_payload({**common, **record})
        for record in payload["events"]  # type: ignore[index]
    )


def test_provider_neutral_fixture_has_stable_categories_and_safe_extensions() -> None:
    events = WorkerTraceProjectionService().normalize_events(raw_facts())

    assert [event.category for event in events] == [
        TraceCategory.INPUT,
        TraceCategory.ASSISTANT_SUMMARY,
        TraceCategory.MODEL,
        TraceCategory.TOOL,
        TraceCategory.SUBTOOL,
        TraceCategory.ARTIFACT,
        TraceCategory.CHECKPOINT,
        TraceCategory.SCHEDULER,
        TraceCategory.ERROR,
        TraceCategory.MODEL,
    ]
    assert events[2].usage is not None
    assert events[2].usage.input_tokens == 12
    assert events[5].artifact_refs == (
        UUID("a0000000-0000-0000-0000-000000000001"),
    )
    assert "authorization" not in events[8].provider_extensions
    assert "absolute_path" not in events[8].provider_extensions
    assert "fencing_token" not in events[7].provider_extensions
    assert all(len(event.title) <= 160 for event in events)
    assert all(len(event.summary) <= 4096 for event in events)


def test_unknown_provider_event_is_readable_and_replay_is_deterministic() -> None:
    service = WorkerTraceProjectionService()
    first = service.normalize_events(raw_facts())
    second = service.normalize_events(raw_facts())

    assert first == second
    assert first[-1].category is TraceCategory.MODEL
    assert first[-1].summary == "future_provider_event"


def test_opencode_mapping_uses_the_same_normalized_contract() -> None:
    fact = RawTraceFact(
        schema_version=1,
        workspace_id=UUID("20000000-0000-0000-0000-000000000001"),
        root_task_id=UUID("30000000-0000-0000-0000-000000000001"),
        plan_snapshot_id=None,
        work_item_key=None,
        execution_id=UUID("70000000-0000-0000-0000-000000000001"),
        session_id=UUID("80000000-0000-0000-0000-000000000001"),
        sequence=1,
        occurred_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        event_type="progress.updated",
        status="running",
        payload={"sessionUpdate": "tool_call", "tool": "git"},
    )

    mapped = WorkerTraceProjectionService().normalize_events((fact,))[0]

    assert mapped.schema_version == 1
    assert mapped.category is TraceCategory.TOOL
    assert mapped.summary == "git"


def test_turn_completion_uses_business_copy_without_protocol_stop_reason() -> None:
    fact = RawTraceFact(
        schema_version=1,
        workspace_id=UUID("20000000-0000-0000-0000-000000000001"),
        root_task_id=UUID("30000000-0000-0000-0000-000000000001"),
        plan_snapshot_id=None,
        work_item_key="backend",
        execution_id=UUID("70000000-0000-0000-0000-000000000001"),
        session_id=UUID("80000000-0000-0000-0000-000000000001"),
        sequence=2,
        occurred_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        event_type="turn.completed",
        status="succeeded",
        payload={
            "schema_version": 1,
            "operation_id": "90000000-0000-0000-0000-000000000001",
            "stop_reason": "end_turn",
        },
    )

    mapped = WorkerTraceProjectionService().normalize_events((fact,))[0]

    assert mapped.title == "回合已完成"
    assert mapped.summary == "Agent 已完成本轮执行"
    assert "end_turn" not in str(mapped.provider_extensions)


TRACE_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
TRACE_ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
TRACE_EXECUTION_ID = UUID("70000000-0000-0000-0000-000000000001")
TRACE_SESSION_ID = UUID("80000000-0000-0000-0000-000000000001")
TRACE_ARTIFACT_ID = UUID("a0000000-0000-0000-0000-000000000001")
TRACE_AT = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _trace_fact(
    sequence: int,
    *,
    event_type: str = "assistant.summary",
    payload: dict[str, object] | None = None,
) -> RawTraceFact:
    return RawTraceFact(
        schema_version=1,
        workspace_id=TRACE_WORKSPACE_ID,
        root_task_id=TRACE_ROOT_TASK_ID,
        plan_snapshot_id=None,
        work_item_key="backend",
        execution_id=TRACE_EXECUTION_ID,
        session_id=TRACE_SESSION_ID,
        sequence=sequence,
        occurred_at=TRACE_AT,
        event_type=event_type,
        status="succeeded",
        payload=payload or {"summary": "Agent completed its work"},
    )


class _TraceStore:
    def __init__(self, snapshot: SimpleNamespace) -> None:
        self.snapshot = snapshot

    def load_trace(self, *args: object, **kwargs: object) -> SimpleNamespace:
        return self.snapshot


def _trace_snapshot(
    *, status: str = "succeeded", events: tuple[RawTraceFact, ...] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        workspace_id=TRACE_WORKSPACE_ID,
        root_task_id=TRACE_ROOT_TASK_ID,
        execution_id=TRACE_EXECUTION_ID,
        session_id=TRACE_SESSION_ID,
        worker_id=None,
        status=status,
        lineage_id=UUID("90000000-0000-0000-0000-000000000001"),
        plan_snapshot_id=None,
        plan_generation=None,
        work_item_key=None,
        worker_slot_key=None,
        events=events or (_trace_fact(11),),
        artifact_refs=(),
        trace_artifacts=(
            SimpleNamespace(
                artifact_id=TRACE_ARTIFACT_ID,
                occurred_at=datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
            ),
        ),
        last_change_cursor=12,
        previous_before_sequence=None,
    )


def test_terminal_trace_projects_session_artifacts_as_trace_events() -> None:
    result = WorkerTraceQueryService(_TraceStore(_trace_snapshot())).get_trace(
        TRACE_WORKSPACE_ID,
        TRACE_ROOT_TASK_ID,
        TRACE_EXECUTION_ID,
        TRACE_SESSION_ID,
        before_sequence=None,
        limit=100,
    )

    artifact_events = [
        event
        for event in result.projection.events
        if event.category is TraceCategory.ARTIFACT
    ]
    assert len(artifact_events) == 1
    assert artifact_events[0].sequence == 12
    assert artifact_events[0].occurred_at == datetime(2026, 8, 21, 12, 1, tzinfo=UTC)
    assert artifact_events[0].artifact_refs == (TRACE_ARTIFACT_ID,)


def test_running_trace_does_not_project_registered_session_artifacts() -> None:
    result = WorkerTraceQueryService(
        _TraceStore(_trace_snapshot(status="running"))
    ).get_trace(
        TRACE_WORKSPACE_ID,
        TRACE_ROOT_TASK_ID,
        TRACE_EXECUTION_ID,
        TRACE_SESSION_ID,
        before_sequence=None,
        limit=100,
    )

    assert all(
        event.category is not TraceCategory.ARTIFACT
        for event in result.projection.events
    )


def test_trace_does_not_duplicate_artifacts_already_referenced_by_events() -> None:
    snapshot = _trace_snapshot(
        events=(
            _trace_fact(
                11,
                event_type="artifact.available",
                payload={"artifact_refs": [str(TRACE_ARTIFACT_ID)]},
            ),
        )
    )
    result = WorkerTraceQueryService(_TraceStore(snapshot)).get_trace(
        TRACE_WORKSPACE_ID,
        TRACE_ROOT_TASK_ID,
        TRACE_EXECUTION_ID,
        TRACE_SESSION_ID,
        before_sequence=None,
        limit=100,
    )

    assert len(result.projection.events) == 1
    assert result.projection.events[0].artifact_refs == (TRACE_ARTIFACT_ID,)


def test_projected_artifact_event_respects_trace_page_limit() -> None:
    result = WorkerTraceQueryService(
        _TraceStore(_trace_snapshot(events=(_trace_fact(11),)))
    ).get_trace(
        TRACE_WORKSPACE_ID,
        TRACE_ROOT_TASK_ID,
        TRACE_EXECUTION_ID,
        TRACE_SESSION_ID,
        before_sequence=None,
        limit=1,
    )

    assert len(result.projection.events) == 1
    assert result.projection.events[0].category is TraceCategory.ARTIFACT
    assert result.projection.previous_before_sequence == 12
