from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from sidera_control_plane.application.collaboration_decisions import (
    CollaborationDecisionApplicationResult,
    CollaborationDecisionConflict,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionRequestModel,
    RuntimeReleaseModel,
)

from .result_input_scenario import ResultInputScenario


@pytest.fixture
def scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_release_fixes_two_sources_once(scenario: ResultInputScenario) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")

    scenario.reconcile()
    first = scenario.selection("summary")
    scenario.reconcile()

    assert scenario.release_count("summary") == 1
    assert scenario.selection("summary").selection_hash == first.selection_hash
    assert tuple(source.worker_item_key for source in first.groups[0].sources) == (
        "b",
        "c",
    )

    with scenario.database.session() as session:
        request = session.get(ExecutionRequestModel, scenario.request_ids["summary"])
        release = session.scalar(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == scenario.workspace_id,
                RuntimeReleaseModel.execution_request_id
                == scenario.request_ids["summary"],
            )
        )
        events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == scenario.workspace_id,
                EventModel.event_type == "CollaborationInputSelectionRecorded",
            )
        ).all()

    assert request is not None
    assert release is not None
    assert request.input_selection_id == first.id
    assert request.input_selection_hash == first.selection_hash
    assert release.input_selection_id == first.id
    assert release.input_selection_hash == first.selection_hash
    assert len(events) == 1
    assert events[0].payload["input_selection_id"] == str(first.id)
    assert "groups" not in events[0].payload


def test_release_fixes_hard_dependency_separately_from_input_group(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan(hard_edges=(("b", "summary"),))
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")

    scenario.reconcile()
    selection = scenario.selection("summary")

    assert tuple(source.worker_item_key for source in selection.groups[0].sources) == (
        "b",
        "c",
    )
    assert len(selection.hard_inputs) == 1
    assert selection.hard_inputs[0].source.worker_item_key == "b"


def _selected_sources(raw: dict[str, object]) -> list[dict[str, Any]]:
    intents = raw["intents"]
    assert isinstance(intents, list)
    groups = intents[0]["input_group_selections"]
    assert isinstance(groups, list)
    sources = groups[0]["sources"]
    assert isinstance(sources, list)
    return sources


def _too_few(raw: dict[str, object]) -> None:
    _selected_sources(raw).pop()


def _too_many(raw: dict[str, object]) -> None:
    sources = _selected_sources(raw)
    invented = deepcopy(sources[0])
    invented["worker_item_key"] = "a"
    invented["priority"] = 1
    sources.insert(0, invented)


def _unready_source(raw: dict[str, object]) -> None:
    source = _selected_sources(raw)[0]
    source["worker_item_key"] = "a"
    source["priority"] = 1


def _foreign_artifact(raw: dict[str, object]) -> None:
    source = _selected_sources(raw)[0]
    foreign_id = str(UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))
    source["artifacts"][0]["artifact_id"] = foreign_id
    source["evidence"][0]["artifact_ids"] = [foreign_id]


def _forged_basis(raw: dict[str, object]) -> None:
    _selected_sources(raw)[0]["source_basis_hash"] = "f" * 64


@pytest.mark.parametrize(
    "mutate",
    (_too_few, _too_many, _unready_source, _foreign_artifact, _forged_basis),
    ids=("too-few", "too-many", "unready", "foreign-artifact", "forged-basis"),
)
def test_invalid_runtime_selection_never_releases_target(
    scenario: ResultInputScenario,
    mutate: Any,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")

    result = scenario.reconcile_mutated(mutate)

    assert result.status == "rejected"
    assert result.applications[0].result_code == "input_selection_invalid"
    assert scenario.release_count("summary") == 0
    assert scenario.selection_count("summary") == 0
    assert scenario.request_selection_ref("summary") == (None, None)


def test_wrong_target_version_is_stale_and_does_not_release(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")

    def change_target_version(raw: dict[str, object]) -> None:
        intents = raw["intents"]
        assert isinstance(intents, list)
        intents[0]["target_request_version"] += 1

    result = scenario.reconcile_mutated(change_target_version)

    assert result.status == "stale"
    assert scenario.release_count("summary") == 0
    assert scenario.selection_count("summary") == 0


def test_precommit_fault_rolls_back_selection_release_pointer_and_can_retry(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    prepared, invocation = scenario.prepare_runtime_result()

    def fail_gate(_uow: Any, _request: Any) -> bool:
        raise RuntimeError("precommit fault")

    with pytest.raises(RuntimeError, match="precommit fault"):
        scenario.apply_runtime_result(
            prepared,
            invocation,
            service=scenario.decision_service(plan_transition_allows=fail_gate),
        )

    assert scenario.selection_count("summary") == 0
    assert scenario.release_count("summary") == 0
    assert scenario.request_selection_ref("summary") == (None, None)

    applied = scenario.apply_runtime_result(prepared, invocation)
    assert applied.status == "applied"
    assert scenario.selection_count("summary") == 1
    assert scenario.release_count("summary") == 1


def test_concurrent_appliers_persist_one_selection_and_release(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
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

    assert (
        sum(
            isinstance(item, CollaborationDecisionApplicationResult) for item in results
        )
        == 1
    )
    assert sum(isinstance(item, CollaborationDecisionConflict) for item in results) == 1
    assert scenario.selection_count("summary") == 1
    assert scenario.release_count("summary") == 1
