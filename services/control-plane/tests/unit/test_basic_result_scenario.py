from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import uuid4

from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.runner import (
    ResultWorkerObservation,
    build_result_worker_evidence,
)


def _builder():
    return EvidenceBuilder(
        run_id=uuid4(),
        scenario="result-inputs",
        evidence_kind="real_worker",
        code_commit="a" * 40,
        runtime_fingerprint="b" * 64,
    )


def _observation(**overrides: object) -> ResultWorkerObservation:
    artifact_body = b"artifact-body"
    downstream_body = b"result marker: RESULT_MARKER"
    source_execution_id = uuid4()
    downstream_execution_id = uuid4()
    observation = ResultWorkerObservation(
        selection_id=uuid4(),
        selection_hash="a" * 64,
        expected_selection_hash="a" * 64,
        bundle_id=uuid4(),
        context_package_id=uuid4(),
        context_package_hash="c" * 64,
        expected_context_package_hash="c" * 64,
        selected_artifact_hashes=(
            ("artifact-1", hashlib.sha256(artifact_body).hexdigest()),
        ),
        artifact_bodies=(("artifact-1", artifact_body),),
        source_execution_ids=(source_execution_id,),
        downstream_execution_id=downstream_execution_id,
        expected_downstream_execution_id=downstream_execution_id,
        downstream_body=downstream_body,
        downstream_marker=b"RESULT_MARKER",
        real_session_ids=(uuid4(),),
        budget_state="completed",
        session_state="succeeded",
    )
    return replace(observation, **overrides)


def test_valid_real_result_evidence_is_pass() -> None:
    report = build_result_worker_evidence(_builder(), _observation())

    assert report.status == "PASS"
    assert report.evidence_kind == "real_worker"
    assert "downstream_marker_observed" in report.check_codes


def test_missing_marker_is_not_pass() -> None:
    report = build_result_worker_evidence(
        _builder(), _observation(downstream_body=b"result without the marker")
    )

    assert report.status == "FAIL"
    assert report.failure_code == "downstream_marker_missing"


def test_wrong_artifact_hash_is_not_pass() -> None:
    report = build_result_worker_evidence(
        _builder(), _observation(selected_artifact_hashes=(("artifact-1", "e" * 64),))
    )

    assert report.status == "FAIL"
    assert report.failure_code == "artifact_hash_mismatch"


def test_wrong_downstream_execution_is_not_pass() -> None:
    report = build_result_worker_evidence(
        _builder(), _observation(expected_downstream_execution_id=uuid4())
    )

    assert report.status == "FAIL"
    assert report.failure_code == "downstream_execution_mismatch"


def test_budget_exhaustion_is_incomplete() -> None:
    report = build_result_worker_evidence(
        _builder(), _observation(budget_state="exhausted")
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "budget_exhausted"


def test_real_session_timeout_is_incomplete() -> None:
    report = build_result_worker_evidence(
        _builder(), _observation(session_state="timed_out")
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "real_session_timed_out"
