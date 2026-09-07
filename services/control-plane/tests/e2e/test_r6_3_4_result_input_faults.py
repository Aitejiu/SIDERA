from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from sidera_control_plane.application.collaboration_decisions import (
    CollaborationDecisionConflict,
)
from sidera_control_plane.application.worker_gateway import GatewayRecoveryRequired
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CollaborationReleaseProposalModel,
    ContextPackageModel,
    ExecutionModel,
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


@pytest.fixture
def started_candidate(
    database, workspace_id: UUID, tmp_path: Path
) -> tuple[ResultInputSeed, ResultInputScenario, object]:
    seed, scenario = seed_result_input_plan(database, workspace_id, tmp_path / "sources")
    scenario.confirm_prepared_plan()
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    scenario.reconcile()
    scenario.activate()
    package = scenario.assemble_context("summary")
    scenario.complete_source("a", body=SOURCE_BODIES["a"])
    return seed, scenario, package


def test_missing_body_recovers_after_rebuilding_scenario(
    database, started_candidate
) -> None:
    seed, scenario, package = started_candidate
    artifact_id = scenario.source_artifact_id("b")
    with database.session() as session:
        artifact = session.get(ArtifactModel, artifact_id)
        assert artifact is not None
        digest = artifact.content_hash

    scenario.make_artifact_unavailable(artifact_id)
    blocked = scenario.start_worker("summary")
    assert getattr(blocked, "code", None) == "result_input_unavailable"
    assert scenario.start_call_count("summary") == 0

    restarted = ResultInputScenario(database, scenario.workspace_id, scenario.tmp_path)
    restarted.now = scenario.now
    restarted.load_materialized_plan(seed.pending.planning_request_id)
    restarted.artifact_store.put(
        restarted.workspace_id,
        restarted.latest_execution_id("b"),
        digest,
        SOURCE_BODIES["b"],
    )
    started = restarted.start_worker("summary")

    assert getattr(started, "session_id", None) is not None
    assert restarted.start_call_count("summary") == 1
    assert restarted.release_count("summary") == 1
    assert restarted.package_ids("summary") == (package.package_id,)
    with database.session() as session:
        summary_execution = session.scalar(
            select(ExecutionModel.id)
            .where(
                ExecutionModel.workspace_id == restarted.workspace_id,
                ExecutionModel.worker_item_id == restarted.worker_item_ids["summary"],
            )
            .order_by(ExecutionModel.created_at.desc())
            .limit(1)
        )
        stored_package = session.get(ContextPackageModel, package.package_id)
        assert summary_execution is not None
        assert stored_package is not None
        assert stored_package.status == "active"


def test_unknown_start_is_not_replayed_after_rebuilding_scenario(
    database, started_candidate
) -> None:
    seed, scenario, _package = started_candidate

    with pytest.raises(GatewayRecoveryRequired):
        scenario.start_worker("summary", start_outcome_unknown=True)
    assert scenario.start_call_count("summary") == 1

    restarted = ResultInputScenario(database, scenario.workspace_id, scenario.tmp_path)
    restarted.now = scenario.now
    restarted.load_materialized_plan(seed.pending.planning_request_id)
    with pytest.raises(GatewayRecoveryRequired):
        restarted.start_worker("summary")
    assert restarted.start_call_count("summary") == 0


def test_selection_commit_failure_rolls_back_and_retries(
    seeded_result_input,
) -> None:
    _seed, scenario = seeded_result_input
    scenario.confirm_prepared_plan()
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    prepared, invocation = scenario.prepare_runtime_result()

    def fail_gate(_uow: Any, _request: Any) -> bool:
        raise RuntimeError("selection commit fault")

    with pytest.raises(RuntimeError, match="selection commit fault"):
        scenario.apply_runtime_result(
            prepared,
            invocation,
            service=scenario.decision_service(plan_transition_allows=fail_gate),
        )
    assert scenario.selection_count("summary") == 0
    assert scenario.release_count("summary") == 0

    applied = scenario.apply_runtime_result(prepared, invocation)
    assert applied.status == "applied"
    assert scenario.selection_count("summary") == 1
    assert scenario.release_count("summary") == 1


def test_two_evaluators_converge_on_one_selection_and_release(
    seeded_result_input,
) -> None:
    _seed, scenario = seeded_result_input
    scenario.confirm_prepared_plan()
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    prepared, invocation = scenario.prepare_runtime_result()

    def apply_once() -> object:
        try:
            return scenario.decision_service().apply(
                prepared, invocation, now=scenario.now
            )
        except CollaborationDecisionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: apply_once(), range(2)))

    assert len(results) == 2
    assert scenario.selection_count("summary") == 1
    assert scenario.release_count("summary") == 1


def test_changed_selected_source_stales_pending_approval_without_new_release(
    database, seeded_result_input
) -> None:
    _seed, scenario = seeded_result_input
    scenario.confirm_prepared_plan()
    scenario.pause_release("summary")
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    scenario.reconcile()
    proposal = scenario.pending_proposal("summary")
    assert proposal.input_selection_id is not None
    with scenario.uow_factory() as uow:
        selection = uow.input_selections.get(
            scenario.workspace_id, proposal.input_selection_id
        )
    assert selection is not None
    selected_artifact_id = selection.groups[0].sources[0].artifacts[0].artifact_id

    with database.session() as session:
        session.execute(
            update(ArtifactModel)
            .where(
                ArtifactModel.workspace_id == scenario.workspace_id,
                ArtifactModel.id == selected_artifact_id,
            )
            .values(content_hash="f" * 64)
        )
        session.commit()

    result = scenario.approve(proposal)

    assert result.data["result"] == "stale"
    assert result.data["stale_reason"] == "input_selection_source_stale"
    assert scenario.release_count("summary") == 0
    with database.session() as session:
        status = session.scalar(
            select(CollaborationReleaseProposalModel.status).where(
                CollaborationReleaseProposalModel.workspace_id
                == scenario.workspace_id,
                CollaborationReleaseProposalModel.id == proposal.id,
            )
        )
    assert status == "stale"


def test_context_dead_end_recovers_locally_without_reselecting_inputs(
    seeded_result_input,
) -> None:
    _seed, scenario = seeded_result_input
    scenario.confirm_prepared_plan()
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    scenario.reconcile()
    selection_hash = scenario.selection("summary").selection_hash
    scenario.activate()
    scenario.mutate_context_assembly_budget(
        "summary",
        lambda budget: budget.update({"execution_context_inheritance": "invalid"}),
    )

    blocked = scenario.run_context_worker("summary")
    assert blocked.blocked == 1
    scenario.mutate_context_assembly_budget(
        "summary",
        lambda budget: budget.pop("execution_context_inheritance", None),
    )
    scenario.retry_context("summary")
    ready = scenario.run_context_worker("summary")
    assert ready.ready == 1
    scenario.assemble_context("summary")
    scenario.complete_source("a", body=SOURCE_BODIES["a"])
    scenario.start_worker("summary")
    assert scenario.selection("summary").selection_hash == selection_hash


def test_result_input_notification_opens_and_recovers_once(
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
    scenario.remove_source_body("b")

    blocked = scenario.start_worker("summary")
    assert getattr(blocked, "code", None) == "result_input_unavailable"
    scenario.run_notifications()
    scenario.run_notifications()
    assert len(scenario.notification_facts("result_inputs_blocked")) == 1

    scenario.restore_artifact(scenario.source_artifact_id("b"))
    started = scenario.start_worker("summary")
    assert getattr(started, "session_id", None) is not None
    scenario.run_notifications()
    scenario.run_notifications()
    facts = scenario.notification_facts("result_inputs_blocked")
    assert sorted(item.transition for item in facts) == ["opened", "recovered"]
