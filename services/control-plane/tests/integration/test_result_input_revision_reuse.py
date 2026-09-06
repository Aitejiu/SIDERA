from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
)
from sidera_control_plane.domain.context_packages import ContextPackageManifestV2
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import WorkerTransitionStatus
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionInputModel,
    ExecutionRequestModel,
)
from tests.integration.result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_revision_reuses_worker_items_and_preserves_running_selection(scenario) -> None:
    scenario.run_to_started_summary()
    old_execution_id = scenario.latest_execution_id("summary")
    old_package = scenario.assemble_context("summary")
    old_selection = scenario.selection("summary")
    old_worker_item_ids = dict(scenario.worker_item_ids)

    revision = scenario.revise_group(minimum_count=1)

    assert revision.data["plan_generation"] == 2
    assert scenario.worker_item_ids == old_worker_item_ids
    assert old_package.result_input_manifest.input_selection_id == old_selection.id
    assert (
        old_package.result_input_manifest.input_selection_hash
        == old_selection.selection_hash
    )

    with scenario.uow_factory() as uow:
        transitions = uow.worker_transitions.list_for_plan_transition(
            UUID(str(revision.data["plan_transition_id"])), scenario.workspace_id
        )
        summary_transition = next(
            item
            for item in transitions
            if item.worker_item_id == old_worker_item_ids["summary"]
        )
        assert summary_transition.action is WorkerTransitionAction.CONTINUE
        assert summary_transition.status is WorkerTransitionStatus.PENDING

        old_execution_request = uow.execution_requests.get(
            scenario.request_ids["summary"], scenario.workspace_id
        )
        assert old_execution_request is not None
        assert old_execution_request.first_execution_id == old_execution_id
        assert old_execution_request.input_selection_id == old_selection.id
        assert old_execution_request.input_selection_hash == old_selection.selection_hash

    assert isinstance(scenario.context_package_for_execution(old_execution_id), ContextPackageManifestV2)


def test_revision_authorizes_new_selection_for_the_future_execution(scenario) -> None:
    scenario.run_to_started_summary()
    old_execution_id = scenario.latest_execution_id("summary")
    old_selection = scenario.selection("summary")
    revision = scenario.revise_group(minimum_count=1)
    scenario.fail_worker("summary")

    coordinator = PlanTransitionCoordinator(
        uow_factory=scenario.uow_factory,
        owner_id="result-input-revision-coordinator",
        clock=scenario.clock,
    )
    for _ in range(8):
        coordinator.run_once(limit=20)
        scenario.reconcile()
        scenario.activate()

    with scenario.database.session() as session:
        new_request_id = session.scalar(
            select(ExecutionRequestModel.id)
            .where(
                ExecutionRequestModel.workspace_id == scenario.workspace_id,
                ExecutionRequestModel.worker_item_id
                == scenario.worker_item_ids["summary"],
                ExecutionRequestModel.plan_generation
                == int(revision.data["plan_generation"]),
            )
            .order_by(ExecutionRequestModel.created_at.desc())
            .limit(1)
        )
    assert new_request_id is not None
    scenario.request_ids["summary"] = new_request_id
    new_execution_id = scenario.latest_execution_id("summary")
    assert new_execution_id != old_execution_id

    new_package = scenario.assemble_context("summary")
    new_selection = scenario.selection("summary")
    assert isinstance(new_package, ContextPackageManifestV2)
    assert new_selection.id != old_selection.id
    assert new_selection.selection_hash != old_selection.selection_hash
    assert len(new_selection.groups) == 1
    assert len(new_selection.groups[0].sources) == 1
    assert (
        new_package.result_input_manifest.input_selection_id == new_selection.id
    )
    scenario.start_worker("summary")
    assert scenario.start_call_count("summary") == 2


def test_local_worker_intervention_keeps_selection_and_records_artifact_ref(
    scenario,
) -> None:
    scenario.run_to_started_summary()
    execution_id = scenario.latest_execution_id("summary")
    package = scenario.assemble_context("summary")
    selection = scenario.selection("summary")

    result = scenario.send_worker_input(
        "summary",
        text="Use the late review note when checking the summary.",
        attachment_refs=("artifact://late-review-note",),
    )

    with scenario.database.session() as session:
        input_row = session.get(ExecutionInputModel, result.aggregate_id)
        event = session.scalar(
            select(EventModel)
            .where(
                EventModel.workspace_id == scenario.workspace_id,
                EventModel.aggregate_type == "execution_input",
                EventModel.aggregate_id == result.aggregate_id,
                EventModel.event_type == "ExecutionInputAccepted",
            )
        )
    assert input_row is not None
    assert input_row.execution_id == execution_id
    assert input_row.status == "queued"
    assert input_row.attachment_refs == ["artifact://late-review-note"]
    assert input_row.source_command_id == result.command_id
    assert event is not None
    assert event.payload["text_length"] == len(
        "Use the late review note when checking the summary."
    )
    assert event.payload["attachment_count"] == 1
    assert "text" not in event.payload
    assert event.payload["execution_context"]["execution_id"] == str(execution_id)

    assert scenario.latest_execution_id("summary") == execution_id
    assert scenario.selection("summary").id == selection.id
    assert scenario.context_package_for_execution(execution_id).package_id == package.package_id
