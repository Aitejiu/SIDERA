from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest

from sidera_control_plane.application.result_input_sources import (
    resolve_result_input_source,
)
from sidera_control_plane.domain.execution import ExecutionStatus

from .result_input_fixtures import effective_source_facts


def test_current_source_uses_exact_plan_placement_and_attempt_facts() -> None:
    source = resolve_result_input_source(
        worker_item_key="a",
        facts=effective_source_facts(request_case="current"),
    )

    assert source.worker_item_key == "a"
    assert source.worker_item_id is not None
    assert source.subtask_path == ("research",)
    assert source.effective_request_id is not None
    assert source.effective_execution_id is not None
    assert source.attempt == 1
    assert source.execution_status is ExecutionStatus.SUCCEEDED
    assert len(source.artifacts) == 1
    assert len(source.evidence) == 1
    assert source.reason_code is None
    with pytest.raises(FrozenInstanceError):
        source.reason_code = "changed"  # type: ignore[misc]


def test_persisted_acceptance_key_is_normalized_to_selector() -> None:
    facts = effective_source_facts(request_case="current")
    raw_evidence = replace(
        facts.evidence[0], acceptance_criterion_ref="research-complete"
    )

    source = resolve_result_input_source(
        worker_item_key="a",
        facts=replace(facts, evidence=(raw_evidence,)),
    )

    assert source.evidence[0].acceptance_criterion_ref == (
        "acceptance:research-complete"
    )


def test_newer_unrelated_request_does_not_replace_effective_source() -> None:
    facts = effective_source_facts(request_case="unrelated_newer_request")
    source = resolve_result_input_source(worker_item_key="a", facts=facts)
    expected = resolve_result_input_source(
        worker_item_key="a",
        facts=effective_source_facts(request_case="current"),
    )

    assert source.effective_request_id == expected.effective_request_id
    assert source.effective_execution_id == expected.effective_execution_id
    assert source.reason_code is None


def test_authorized_continuation_extends_allowed_lineage_explicitly() -> None:
    source = resolve_result_input_source(
        worker_item_key="a",
        facts=effective_source_facts(request_case="authorized_continuation"),
    )

    assert source.effective_execution_id == source.allowed_lineage_ids[-1]
    assert len(source.allowed_lineage_ids) == 2
    assert source.inheritance_refs[0].startswith("continuation:")
    assert source.reason_code is None


def test_converged_handoff_retires_source_and_uses_bound_target() -> None:
    current = resolve_result_input_source(
        worker_item_key="a",
        facts=effective_source_facts(request_case="current"),
    )
    handed_off = resolve_result_input_source(
        worker_item_key="a",
        facts=effective_source_facts(request_case="authorized_handoff"),
    )

    assert handed_off.effective_request_id != current.effective_request_id
    assert handed_off.effective_execution_id != current.effective_execution_id
    assert len(handed_off.allowed_lineage_ids) == 2
    assert handed_off.inheritance_refs[0].startswith("handoff:")
    assert all(
        artifact.artifact_id not in {item.artifact_id for item in current.artifacts}
        for artifact in handed_off.artifacts
    )


def test_two_current_requests_are_ambiguous_instead_of_uuid_ordered() -> None:
    source = resolve_result_input_source(
        worker_item_key="a",
        facts=effective_source_facts(request_case="ambiguous_current"),
    )

    assert source.effective_request_id is None
    assert source.effective_execution_id is None
    assert source.allowed_lineage_ids == ()
    assert source.outcome_unknown is True
    assert source.reason_code == "input_source_ambiguous"


def test_current_plan_request_takes_precedence_over_historical_source_request() -> None:
    facts = effective_source_facts(request_case="current")
    current_request = facts.requests[0]
    historical_request = replace(
        current_request,
        id=UUID("6d4a0902-3a0e-4fbc-b3ec-47c717b3e01a"),
        execution_lineage_id=UUID("f41ab4dd-41d7-4a0d-9e27-883a77f75eca"),
        first_execution_id=UUID("f41ab4dd-41d7-4a0d-9e27-883a77f75eca"),
    )

    source = resolve_result_input_source(
        worker_item_key="a",
        facts=replace(facts, result_source_requests=(historical_request,)),
    )

    assert source.effective_request_id == current_request.id
    assert source.effective_execution_id == current_request.first_execution_id
    assert source.reason_code is None


def test_failed_source_preserves_retry_possibility_without_fake_attempt() -> None:
    facts = effective_source_facts(request_case="current")
    failed = replace(facts.executions[0], status="failed", attempt=1, max_attempts=2)
    source = resolve_result_input_source(
        worker_item_key="a",
        facts=replace(facts, executions=(failed,)),
    )

    assert source.attempt == 1
    assert source.execution_status is ExecutionStatus.FAILED
    assert source.retry_possible is True
    assert source.outcome_unknown is False


def test_unknown_worker_does_not_receive_default_id_or_attempt() -> None:
    source = resolve_result_input_source(
        worker_item_key="missing",
        facts=effective_source_facts(request_case="current"),
    )

    assert source.worker_item_id is None
    assert source.effective_request_id is None
    assert source.effective_execution_id is None
    assert source.attempt is None
    assert source.reason_code == "input_source_not_found"
