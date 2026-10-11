import json
from dataclasses import replace
from datetime import timedelta
from importlib import import_module
from uuid import UUID

import pytest
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.schemas import WorkerWorkspaceProjectionResponse
from sidera_control_plane.application.worker_workspace_queries import (
    WorkerWorkspaceQueryService,
    WorkerWorkspaceQuerySnapshot,
)
from sidera_control_plane.persistence.models import EventModel
from tests.unit.test_worker_workspace_projection import (
    EXECUTION_ONE_ID,
    EXECUTION_TWO_ID,
    NOW,
    ROOT_TASK_ID,
    WORKER_ITEM_ID,
    WORKSPACE_ID,
    _executions,
    _service,
)


def report(number, *, execution_id=EXECUTION_TWO_ID, binding_version=3, minute=9):
    module = import_module("sidera_control_plane.domain.worker_item_projections")
    return module.WorkerProgressFact(
        report_id=UUID(int=number),
        event_id=UUID(int=number),
        execution_id=execution_id,
        binding_version=binding_version,
        run_id=UUID(int=901),
        response_id=UUID(int=902),
        summary=f"Worker report {number}",
        steps=({"stage": "inspect", "finished": True, "labels": ["100%", None]},),
        reported_at=NOW + timedelta(minutes=minute),
        verified=False,
    )


def test_latest_report_requires_selected_execution_and_current_native_binding():
    reports = (
        report(1),
        report(2, minute=10),
        report(3, binding_version=2, minute=11),
        report(4, execution_id=EXECUTION_ONE_ID, minute=12),
    )
    baseline = _service().compose_workspace()
    current = replace(_executions()[1], native_binding_version=3)
    service = _service(executions=(_executions()[0], current), progress_reports=reports)
    workspace = service.compose_workspace()
    assert workspace.current_progress.report_id == UUID(int=2)
    assert workspace.current_progress.event_id == UUID(int=2)
    assert workspace.current_progress.execution_id == EXECUTION_TWO_ID
    assert (
        workspace.current_progress.binding_version == 3
        and workspace.current_progress.verified is False
    )
    assert workspace.current_progress.steps == reports[1].steps
    assert workspace.current_step == baseline.current_step
    assert (
        workspace.latest_checkpoint == baseline.latest_checkpoint
        and workspace.usage == baseline.usage
    )
    assert workspace.trace_descriptor == baseline.trace_descriptor
    assert workspace.intervention_context == baseline.intervention_context
    assert workspace.worker_item == baseline.worker_item
    assert workspace.current_runtime == baseline.current_runtime
    response = WorkerWorkspaceProjectionResponse.model_validate(
        jsonable_encoder(workspace)
    )
    assert (
        response.current_progress.report_id == UUID(int=2)
        and response.current_progress.verified is False
    )
    activity = service.compose_activity(service.activity_facts(), limit=200)
    history = [item for item in activity.items if item.source_type == "progress_report"]
    assert len(history) == 4
    assert {item.source_id for item in history} == {
        UUID(int=value) for value in (1, 2, 3, 4)
    }
    for item in history:
        assert (
            item.title == "Progress report"
            and item.session_id is None
            and item.trace_descriptor is None
        )
        assert json.loads(item.body)["verified"] is False
        assert json.loads(item.body)["steps"] == [
            {"stage": "inspect", "finished": True, "labels": ["100%", None]}
        ]
    assert service.compose_activity(service.activity_facts(), limit=200) == activity
    first_page = service.compose_activity(service.activity_facts(), limit=1)
    following = service.compose_activity(
        service.activity_facts(), limit=200, after=first_page.next_cursor
    )
    assert (first_page.items + following.items) == activity.items


@pytest.mark.parametrize(
    "changed", ("no_native_authority", "new_binding", "new_execution")
)
def test_old_report_history_is_not_promoted_after_native_ownership_changes(changed):
    reports = (report(1),)
    execution = replace(
        _executions()[1],
        native_binding_version=None if changed == "no_native_authority" else 4,
    )
    if changed == "new_execution":
        execution = replace(
            execution, id=UUID(int=903), status="queued", started_at=None
        )
    service = _service(executions=(execution,), progress_reports=reports)
    assert service.compose_workspace().current_progress is None
    assert (
        len(
            [
                item
                for item in service.activity_facts()
                if item.source_type == "progress_report"
            ]
        )
        == 1
    )


def test_progress_query_defaults_preserve_existing_workspace_and_activity():
    service = _service()
    assert service.compose_workspace().current_progress is None
    assert all(
        item.source_type != "progress_report" for item in service.activity_facts()
    )


def test_public_workspace_query_forwards_saved_report_facts_to_workspace_and_activity():
    from types import SimpleNamespace

    baseline = _service()
    snapshot = WorkerWorkspaceQuerySnapshot(
        worker_item=baseline.worker_item,
        placements=baseline.placements,
        executions=(replace(_executions()[1], native_binding_version=3),),
        sessions=baseline.sessions,
        workers=baseline.workers,
        bindings=baseline.bindings,
        leases=baseline.leases,
        events=baseline.events,
        inputs=baseline.inputs,
        artifacts=baseline.artifacts,
        evidence=baseline.evidence,
        last_change_cursor=baseline.last_change_cursor,
        as_of=baseline.as_of,
        progress_reports=(report(1),),
    )
    query = WorkerWorkspaceQueryService(SimpleNamespace(load=lambda *args: snapshot))
    assert query.get_workspace(
        WORKSPACE_ID, WORKER_ITEM_ID
    ).current_progress.report_id == UUID(int=1)
    activity = query.list_activity(WORKSPACE_ID, WORKER_ITEM_ID, after=None, limit=200)
    assert [
        item.source_id
        for item in activity.items
        if item.source_type == "progress_report"
    ] == [UUID(int=1)]


def stored_report():
    return EventModel(
        id=UUID(int=1),
        workspace_id=WORKSPACE_ID,
        aggregate_type="worker_progress_report",
        aggregate_id=UUID(int=1),
        aggregate_version=1,
        event_type="WorkerProgressReported",
        actor_type="agent",
        actor_id="operator",
        causation_id=UUID(int=5),
        correlation_id=ROOT_TASK_ID,
        payload={
            "report_id": str(UUID(int=1)),
            "execution_id": str(EXECUTION_TWO_ID),
            "execution_version": 7,
            "worker_item_id": str(WORKER_ITEM_ID),
            "root_task_id": str(ROOT_TASK_ID),
            "binding_version": 3,
            "run_id": str(UUID(int=901)),
            "response_id": str(UUID(int=902)),
            "summary": "Actual saved report",
            "steps": [{"stage": "inspect"}],
            "reported_at": (NOW + timedelta(minutes=9)).isoformat(),
            "verified": False,
        },
    )


def test_progress_parser_reads_the_business_fact_not_a_native_execution_event():
    module = import_module(
        "sidera_control_plane.persistence.worker_workspace_query_store"
    )
    row = stored_report()
    fact = module._progress_report(row)
    assert (
        fact.report_id == fact.event_id == row.id
        and fact.execution_id == EXECUTION_TWO_ID
    )
    assert fact.binding_version == 3 and fact.verified is False
    assert fact.summary == "Actual saved report" and fact.steps == (
        {"stage": "inspect"},
    )
    assert fact.reported_at == NOW + timedelta(minutes=9)


@pytest.mark.parametrize(
    "invalid",
    (
        "aggregate",
        "event_type",
        "report_identity",
        "binding_bool",
        "verified",
        "naive_time",
    ),
)
def test_progress_parser_rejects_corrupted_or_foreign_event_shape(invalid):
    module = import_module(
        "sidera_control_plane.persistence.worker_workspace_query_store"
    )
    row = stored_report()
    if invalid == "aggregate":
        row.aggregate_type = "execution"
    elif invalid == "event_type":
        row.event_type = "ExecutionStarted"
    elif invalid == "report_identity":
        row.payload["report_id"] = str(UUID(int=9))
    elif invalid == "binding_bool":
        row.payload["binding_version"] = True
    elif invalid == "verified":
        row.payload["verified"] = True
    else:
        row.payload["reported_at"] = NOW.replace(tzinfo=None).isoformat()
    with pytest.raises(ValueError):
        module._progress_report(row)
