from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import update

from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.domain.errors import ResultInputsDisabled
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionRequestModel,
    RuntimeReleaseModel,
)

from .result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_quorum_does_not_bypass_independent_hard_dependency(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan(hard_edges=(("a", "summary"),))
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")

    scenario.reconcile()
    scenario.activate()

    assert scenario.release_count("summary") == 0
    assert scenario.execution_count("summary") == 0
    assert scenario.request_state("summary") == "blocked"

    scenario.complete_source("a", body=b"report-a\n")
    scenario.reconcile()
    scenario.activate()

    assert scenario.release_count("summary") == 1
    assert scenario.execution_count("summary") == 1
    assert scenario.request_state("summary") == "materialized"


def test_v5_confirmation_is_rejected_when_new_result_inputs_are_disabled(
    scenario: ResultInputScenario,
) -> None:
    with pytest.raises(ResultInputsDisabled) as error:
        scenario.confirm_plan(result_inputs_enabled=False)

    assert error.value.code == "result_inputs_disabled"
    assert error.value.details["setting"] == "SIDERA_RESULT_INPUTS_ENABLED"


def test_all_input_groups_must_be_satisfied_before_activation(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan(
        groups=(
            {
                "group_key": "research",
                "title": "Research results",
                "consumer_worker_item_key": "summary",
                "candidates": [
                    {"worker_item_key": "b", "priority": 1},
                    {"worker_item_key": "c", "priority": 2},
                ],
                "requirements": {
                    "execution_status": "succeeded",
                    "required_artifact_refs": ["artifact_type:build"],
                    "required_evidence_refs": ["acceptance:build"],
                    "evidence_status": "candidate",
                },
                "mode": "all",
                "selection_strategy": "plan_order",
            },
            {
                "group_key": "approval-notes",
                "title": "Approval notes",
                "consumer_worker_item_key": "summary",
                "candidates": [{"worker_item_key": "a", "priority": 1}],
                "requirements": {
                    "execution_status": "succeeded",
                    "required_artifact_refs": ["artifact_type:build"],
                    "required_evidence_refs": ["acceptance:build"],
                    "evidence_status": "candidate",
                },
                "mode": "all",
                "selection_strategy": "plan_order",
            },
        )
    )
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")

    scenario.reconcile()
    scenario.activate()

    assert scenario.release_count("summary") == 0
    assert scenario.execution_count("summary") == 0


def test_hold_blocks_activation_until_the_fixed_selection_is_approved(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.pause_release("summary")
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()

    proposal = scenario.pending_proposal("summary")
    scenario.activate()

    assert scenario.release_count("summary") == 0
    assert scenario.execution_count("summary") == 0

    scenario.approve(proposal)
    scenario.activate()

    assert scenario.release_count("summary") == 1
    assert scenario.execution_count("summary") == 1


def test_activation_rejects_release_whose_selection_hash_is_not_the_ledger_hash(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()
    forged_hash = "f" * 64

    with scenario.database.session() as session:
        session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == scenario.workspace_id,
                ExecutionRequestModel.id == scenario.request_ids["summary"],
            )
            .values(input_selection_hash=forged_hash)
        )
        session.execute(
            update(RuntimeReleaseModel)
            .where(
                RuntimeReleaseModel.workspace_id == scenario.workspace_id,
                RuntimeReleaseModel.execution_request_id
                == scenario.request_ids["summary"],
            )
            .values(input_selection_hash=forged_hash)
        )
        session.commit()

    scenario.activate()

    assert scenario.execution_count("summary") == 0
    assert scenario.request_state("summary") == "blocked"


def test_late_unselected_candidate_does_not_change_release_or_request_version(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()
    selection_ref = scenario.request_selection_ref("summary")
    request_version = scenario.request_version("summary")

    scenario.complete_source("a", body=b"late-a\n")
    scenario.reconcile()
    scenario.reconcile()

    assert scenario.release_count("summary") == 1
    assert scenario.request_selection_ref("summary") == selection_ref
    assert scenario.request_version("summary") == request_version


def test_selection_bound_activation_does_not_reaggregate_hard_input_lineages(
    scenario: ResultInputScenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario.confirm_plan(hard_edges=(("a", "summary"),))
    scenario.complete_source("a", body=b"report-a\n")
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()

    def reject_legacy_aggregation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("selection-bound activation used legacy lineage aggregation")

    monkeypatch.setattr(
        ActivationReconciler,
        "_fact_for_upstream",
        reject_legacy_aggregation,
    )

    scenario.activate()

    assert scenario.execution_count("summary") == 1
    assert scenario.request_state("summary") == "materialized"
