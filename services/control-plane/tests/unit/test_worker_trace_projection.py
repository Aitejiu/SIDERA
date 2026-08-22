import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sidera_control_plane.application.worker_trace_queries import (
    RawTraceFact,
    WorkerTraceProjectionService,
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
