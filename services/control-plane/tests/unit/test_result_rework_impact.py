from __future__ import annotations

from uuid import UUID

import pytest

from sidera_control_plane.application.result_rework import compute_rework_impact

from .result_integration_fixtures import ReworkImpactCase, rework_impact_case


@pytest.fixture
def impact_case() -> ReworkImpactCase:
    return rework_impact_case()


def test_rework_only_marks_actual_dependents(impact_case: ReworkImpactCase) -> None:
    impact = compute_rework_impact(
        changed_result_id=impact_case.a1.id,
        replacement=impact_case.a2,
        provenance=impact_case.provenance,
        hard_edges=impact_case.hard_edges,
    )

    assert impact.affected == (impact_case.b_worker_id,)
    assert impact.reusable == (impact_case.c_worker_id,)
    assert impact.requires_revision is False
    assert impact.reason_by_worker[impact_case.b_worker_id] == "consumes_changed_result"


def test_same_code_and_valid_input_keeps_actual_consumer_reusable() -> None:
    case = rework_impact_case(replacement_tree="d" * 40)

    impact = compute_rework_impact(
        changed_result_id=case.a1.id,
        replacement=case.a2,
        provenance=case.provenance,
        hard_edges=case.hard_edges,
    )

    assert impact.affected == ()
    assert impact.reusable == (case.b_worker_id, case.c_worker_id)
    assert impact.requires_revision is False
    assert impact.reason_by_worker[case.b_worker_id] == "same_code_and_input"


def test_unknown_information_relation_is_pending_without_plan_revision() -> None:
    case = rework_impact_case(b_consumes_a1=False)

    impact = compute_rework_impact(
        changed_result_id=case.a1.id,
        replacement=case.a2,
        provenance=case.provenance,
        hard_edges=case.hard_edges,
    )

    assert impact.affected == (case.b_worker_id,)
    assert impact.reusable == (case.c_worker_id,)
    assert impact.requires_revision is False
    assert impact.reason_by_worker[case.b_worker_id] == "dependency_provenance_unknown"


def test_external_hard_edge_requires_revision(impact_case: ReworkImpactCase) -> None:
    external_worker_id = UUID("40000000-0000-0000-0000-000000000199")
    impact = compute_rework_impact(
        changed_result_id=impact_case.a1.id,
        replacement=impact_case.a2,
        provenance=impact_case.provenance,
        hard_edges=impact_case.hard_edges + ((impact_case.a_worker_id, external_worker_id),),
    )

    assert impact.requires_revision is True
    assert external_worker_id in impact.affected
    assert impact.reason_by_worker[external_worker_id] == "hard_edge_target_missing"


def test_transitive_hard_dependents_are_stably_ordered(impact_case: ReworkImpactCase) -> None:
    downstream_worker_id = UUID("40000000-0000-0000-0000-000000000199")
    impact = compute_rework_impact(
        changed_result_id=impact_case.a1.id,
        replacement=impact_case.a2,
        provenance=impact_case.provenance,
        hard_edges=impact_case.hard_edges + ((impact_case.b_worker_id, downstream_worker_id),),
    )

    assert impact.affected == (impact_case.b_worker_id, downstream_worker_id)
    assert impact.reason_by_worker[downstream_worker_id] == "hard_edge_target_missing"
