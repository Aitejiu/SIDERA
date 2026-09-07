from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from sidera_control_plane.domain.context_packages import ContextPurpose
from sidera_control_plane.domain.gateway import (
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.persistence.models import (
    CollaborationInputSelectionModel,
    ContextPackageAcknowledgementModel,
    ExecutionModel,
    ExecutionRequestModel,
    GatewayOperationModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    ProposalMaterializationModel,
    RuntimeReleaseModel,
    TaskModel,
)
from tests.e2e.seed_r6_3_4_result_inputs import (
    SOURCE_BODIES,
    ResultInputSeed,
    seed_result_input_plan,
)
from tests.integration.result_input_scenario import ResultInputScenario

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]


@pytest.fixture
def seeded_result_input(
    database, workspace_id: UUID, tmp_path: Path
) -> tuple[ResultInputSeed, ResultInputScenario]:
    return seed_result_input_plan(database, workspace_id, tmp_path / "sources")


def test_seed_stops_before_confirmation_and_runtime_materialization(
    database, seeded_result_input
) -> None:
    seed, _scenario = seeded_result_input
    with database.session() as session:
        request = session.get(PlanningRequestModel, seed.pending.planning_request_id)
        proposal = session.get(
            PlanningProposalRevisionModel, seed.pending.proposal_id
        )
        assert request is not None
        assert request.status == "proposal_ready"
        assert proposal is not None
        assert proposal.content_hash == seed.pending.proposal_content_hash
        assert (
            session.scalar(
                select(func.count()).select_from(ProposalMaterializationModel)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(CollaborationInputSelectionModel)
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(RuntimeReleaseModel)) == 0
        assert (
            session.scalar(
                select(func.count()).select_from(ContextPackageAcknowledgementModel)
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(ExecutionModel)) == 0
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 0

    assert {
        key: path.read_bytes() for key, path in seed.source_files.items()
    } == dict(SOURCE_BODIES)


def test_full_path_delivers_selected_content_and_preserves_siblings(
    database, seeded_result_input
) -> None:
    _seed, scenario = seeded_result_input
    scenario.confirm_prepared_plan()
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    scenario.reconcile()
    selection = scenario.selection("summary")
    scenario.activate()
    package = scenario.assemble_context("summary")
    scenario.complete_source("a", body=SOURCE_BODIES["a"])

    started = scenario.start_worker("summary")

    assert getattr(started, "session_id", None) is not None
    selected_artifacts = {
        (
            artifact.artifact_id,
            artifact.content_hash,
            artifact.size_bytes,
        )
        for group in selection.groups
        for source in group.sources
        for artifact in source.artifacts
    }
    manifest_artifacts = {
        (artifact.artifact_id, artifact.content_hash, artifact.size_bytes)
        for artifact in package.result_input_manifest.artifacts
    }
    captured = scenario.captured_context("summary")
    captured_artifacts = {
        (item.artifact_id, item.content_hash, item.size_bytes)
        for item in captured.result_inputs
    }
    assert selected_artifacts == manifest_artifacts == captured_artifacts
    assert {item.body_text.encode() for item in captured.result_inputs} == {
        SOURCE_BODIES["b"],
        SOURCE_BODIES["c"],
    }
    assert {
        source.worker_item_key
        for group in selection.groups
        for source in group.sources
    } == {"b", "c"}
    assert scenario.start_call_count("summary") == 1
    assert scenario.release_count("summary") == 1
    assert scenario.selection_count("summary") == 1
    assert scenario.package_ids("summary") == (package.package_id,)

    with database.session() as session:
        acknowledgement = session.scalar(
            select(ContextPackageAcknowledgementModel).where(
                ContextPackageAcknowledgementModel.workspace_id
                == scenario.workspace_id,
                ContextPackageAcknowledgementModel.package_id == package.package_id,
                ContextPackageAcknowledgementModel.consumer_type == "worker",
                ContextPackageAcknowledgementModel.consumer_id
                == scenario.latest_execution_id("summary"),
            )
        )
        root_task = session.get(TaskModel, scenario.ids["root_task"])
        assert acknowledgement is not None
        assert acknowledgement.consumer_id == scenario.latest_execution_id("summary")
        assert acknowledgement.acknowledged_package_hash == package.package_hash
        assert acknowledgement.context_version == captured.metadata.context_version
        operation = session.scalar(
            select(GatewayOperationModel).where(
                GatewayOperationModel.workspace_id == scenario.workspace_id,
                GatewayOperationModel.execution_id
                == scenario.latest_execution_id("summary"),
                GatewayOperationModel.operation_type
                == GatewayOperationType.START_SESSION.value,
            )
        )
        assert operation is not None
        assert operation.status == "succeeded"
        assert operation.operation_id == gateway_operation_id(
            scenario.latest_execution_id("summary"),
            GatewayOperationType.START_SESSION,
        )
        assert root_task is not None
        assert root_task.status not in {"completed", "succeeded", "failed"}

        for key in ("b", "c"):
            request = session.get(ExecutionRequestModel, scenario.request_ids[key])
            execution = session.scalar(
                select(ExecutionModel)
                .where(
                    ExecutionModel.workspace_id == scenario.workspace_id,
                    ExecutionModel.worker_item_id == scenario.worker_item_ids[key],
                )
                .order_by(ExecutionModel.created_at.desc())
                .limit(1)
            )
            assert request is not None
            assert request.status not in {"cancelled", "obsolete"}
            assert execution is not None
            assert execution.status == "succeeded"

    with scenario.uow_factory() as uow:
        stored_package = uow.context_packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            scenario.latest_execution_id("summary"),
            scenario.workspace_id,
            manifest_schema_version=2,
        )
    assert stored_package is not None
    assert stored_package.package_id == package.package_id


def test_all_group_materializes_every_ready_candidate(
    database, workspace_id: UUID, tmp_path: Path
) -> None:
    _seed, scenario = seed_result_input_plan(
        database,
        workspace_id,
        tmp_path / "sources",
        groups=(_group("all-results", ("a", "b", "c"), mode="all"),),
    )
    scenario.confirm_prepared_plan()
    for key in ("a", "b", "c"):
        scenario.complete_source(key, body=SOURCE_BODIES[key])

    scenario.reconcile()

    selection = scenario.selection("summary")
    assert {
        source.worker_item_key
        for group in selection.groups
        for source in group.sources
    } == {"a", "b", "c"}
    assert scenario.release_count("summary") == 1


def test_multiple_groups_and_hard_dependency_share_one_release(
    database, workspace_id: UUID, tmp_path: Path
) -> None:
    groups = (
        _group("research", ("b", "c"), mode="all"),
        _group("approval-notes", ("a",), mode="all"),
    )
    _seed, scenario = seed_result_input_plan(
        database,
        workspace_id,
        tmp_path / "sources",
        groups=groups,
        hard_edges=(("a", "summary"),),
    )
    scenario.confirm_prepared_plan()
    for key in ("a", "b", "c"):
        scenario.complete_source(key, body=SOURCE_BODIES[key])

    scenario.reconcile()

    selection = scenario.selection("summary")
    assert {item.group_key for item in selection.groups} == {
        "research",
        "approval-notes",
    }
    assert len(selection.hard_inputs) == 1
    assert selection.hard_inputs[0].source.worker_item_key == "a"
    assert scenario.release_count("summary") == 1


def test_manual_pause_and_approve_keeps_release_blocked_until_review(
    seeded_result_input,
) -> None:
    _seed, scenario = seeded_result_input
    scenario.confirm_prepared_plan()
    scenario.pause_release("summary")
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    scenario.reconcile()

    proposal = scenario.pending_proposal("summary")
    scenario.activate()
    assert scenario.release_count("summary") == 0
    assert scenario.execution_count("summary") == 0

    scenario.approve(proposal)
    scenario.activate()
    assert scenario.release_count("summary") == 1
    assert scenario.execution_count("summary") == 1


def test_late_unselected_source_does_not_change_pending_selection(
    seeded_result_input,
) -> None:
    _seed, scenario = seeded_result_input
    scenario.confirm_prepared_plan()
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    scenario.reconcile()
    selection_ref = scenario.request_selection_ref("summary")
    request_version = scenario.request_version("summary")
    assert scenario.package_ids("summary") == ()

    scenario.complete_source("a", body=SOURCE_BODIES["a"])
    scenario.reconcile()
    scenario.reconcile()

    assert scenario.request_selection_ref("summary") == selection_ref
    assert scenario.request_version("summary") == request_version
    assert scenario.release_count("summary") == 1


def test_retry_reuses_selection_but_creates_a_new_consumer_package(
    seeded_result_input,
) -> None:
    _seed, scenario = seeded_result_input
    scenario.confirm_prepared_plan()
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    scenario.reconcile()
    scenario.activate()
    scenario.assemble_context("summary")
    scenario.complete_source("a", body=SOURCE_BODIES["a"])
    scenario.start_worker("summary")
    selection_hash = scenario.selection("summary").selection_hash
    first_execution_id = scenario.latest_execution_id("summary")
    first_packages = scenario.package_ids("summary")

    scenario.fail_worker("summary")
    scenario.retry_worker("summary")
    scenario.assemble_context("summary")
    scenario.start_worker("summary")

    assert scenario.selection("summary").selection_hash == selection_hash
    assert scenario.latest_execution_id("summary") != first_execution_id
    assert scenario.start_call_count("summary") == 2
    assert len(scenario.package_ids("summary")) == len(first_packages) + 1


def _group(
    group_key: str, candidates: tuple[str, ...], *, mode: str
) -> dict[str, object]:
    return {
        "group_key": group_key,
        "title": group_key,
        "consumer_worker_item_key": "summary",
        "candidates": [
            {"worker_item_key": key, "priority": index}
            for index, key in enumerate(candidates, start=1)
        ],
        "requirements": {
            "execution_status": "succeeded",
            "required_artifact_refs": ["artifact_type:build"],
            "required_evidence_refs": ["acceptance:build"],
            "evidence_status": "candidate",
        },
        "mode": mode,
        "selection_strategy": "plan_order",
    }
