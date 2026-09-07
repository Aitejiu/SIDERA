from __future__ import annotations

from copy import deepcopy

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.result_integration import (
    CriterionSpecV1,
    IntegrationSpecV1,
)
from sidera_control_plane.domain.result_verification import (
    CriterionDecisionType,
    CriterionDecisionV1,
    CriterionObservedStatus,
    VerificationManifestV1,
    VerificationResultV1,
    evaluate_criteria,
)

from .result_integration_fixtures import (
    acceptance_mapping,
    integration_mapping,
    verification_manifest_mapping,
    verification_mapping,
)


def test_exception_does_not_rewrite_observed_failure() -> None:
    spec = IntegrationSpecV1.from_mapping(integration_mapping())
    failed = VerificationResultV1.from_mapping(verification_mapping())
    decision = CriterionDecisionV1(
        "login", CriterionDecisionType.EXCEPTION_ACCEPTED, "Known fixture limitation"
    )

    result = evaluate_criteria(spec.criteria, (failed,), (decision,))

    assert result[0].observed_status is CriterionObservedStatus.FAILED
    assert result[0].failure_codes == ("check_exit_nonzero",)
    assert failed.status.value == "failed"


def test_passed_and_missing_checks_have_distinct_observed_status() -> None:
    spec = IntegrationSpecV1.from_mapping(integration_mapping())
    passed = VerificationResultV1.from_mapping(
        verification_mapping(status="passed", exit_code=0, failure_code=None)
    )

    passed_evaluation = evaluate_criteria(spec.criteria, (passed,))
    pending_evaluation = evaluate_criteria(spec.criteria, ())

    assert passed_evaluation[0].observed_status is CriterionObservedStatus.PASSED
    assert pending_evaluation[0].observed_status is CriterionObservedStatus.PENDING


def test_manual_criterion_is_eligible_for_manual_decision() -> None:
    criterion = CriterionSpecV1.from_mapping(
        {
            "key": "ux",
            "text": "The interaction is understandable",
            "mode": "manual",
            "check_keys": [],
        }
    )

    evaluation = evaluate_criteria((criterion,), ())

    assert evaluation[0].eligible_manual is True
    assert evaluation[0].observed_status is CriterionObservedStatus.PENDING


def test_empty_exception_reason_and_automatic_manual_pass_are_rejected() -> None:
    with pytest.raises(ValidationError, match="exception acceptance requires a reason"):
        CriterionDecisionV1("login", CriterionDecisionType.EXCEPTION_ACCEPTED, " ")

    criterion = CriterionSpecV1.from_mapping(
        {
            "key": "login",
            "text": "Expired credentials are rejected",
            "mode": "automatic",
            "check_keys": ["unit"],
        }
    )
    failed = VerificationResultV1.from_mapping(verification_mapping())
    with pytest.raises(ValidationError, match="manual_passed"):
        evaluate_criteria(
            (criterion,),
            (failed,),
            (
                CriterionDecisionV1(
                    "login", CriterionDecisionType.MANUAL_PASSED, "User checked it"
                ),
            ),
        )


def test_verification_manifest_rejects_a_changed_candidate_basis() -> None:
    raw = verification_manifest_mapping()
    value = deepcopy(raw)
    value["result_tree"] = "e" * 40

    with pytest.raises(ValidationError, match="basis hash"):
        VerificationManifestV1.from_mapping(value)


def test_verification_and_acceptance_contracts_round_trip() -> None:
    manifest = VerificationManifestV1.from_mapping(verification_manifest_mapping())
    result = VerificationResultV1.from_mapping(verification_mapping())

    assert manifest.to_mapping() == verification_manifest_mapping()
    assert result.to_mapping() == verification_mapping()

    from sidera_control_plane.domain.result_verification import TaskAcceptanceDecisionV1

    acceptance = TaskAcceptanceDecisionV1.from_mapping(acceptance_mapping())
    assert acceptance.to_mapping() == acceptance_mapping()
