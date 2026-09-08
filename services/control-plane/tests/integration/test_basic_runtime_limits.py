from __future__ import annotations

from uuid import uuid4

from tests.e2e.basic_acceptance.contracts import RuntimeLimitEvidence
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.recovery import build_runtime_limit_evidence


def test_two_worker_capacity_is_a_hard_runtime_observation() -> None:
    builder = EvidenceBuilder(
        run_id=uuid4(),
        scenario="recovery",
        evidence_kind="contract",
        code_commit="a" * 40,
        runtime_fingerprint="b" * 64,
    )
    report = build_runtime_limit_evidence(
        builder,
        RuntimeLimitEvidence(
            run_id=uuid4(),
            configured_worker_capacity=2,
            observed_peak_worker_capacity=2,
            configured_retry_limit=1,
            observed_retry_count=1,
            session_deadline_seconds=600,
            observed_elapsed_seconds=600,
            deadline_preserved_across_restart=True,
            unknown_relaunch_blocked=True,
            usage_status="available",
        ),
    )

    assert report.status == "PASS"
    assert "worker_capacity_respected" in report.check_codes
    assert "session_deadline_respected" in report.check_codes
