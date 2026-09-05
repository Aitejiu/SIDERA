from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.collaboration_decisions import (
    CollaborationGateFact,
    ComposedActivationGate,
)
from sidera_control_plane.domain.artifacts import EvidenceClaimStatus
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ActivationGate,
    DependencyEnforcement,
    ExecutionRequestDependency,
    UpstreamActivationFact,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
DOWNSTREAM_ID = UUID("29000000-0000-0000-0000-000000000001")


def fact(
    request_id: UUID,
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    artifacts: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
) -> UpstreamActivationFact:
    return UpstreamActivationFact(
        request_id=request_id,
        execution_status=status,
        execution_lineage_id=uuid4() if status is ExecutionStatus.SUCCEEDED else None,
        artifact_refs=artifacts,
        evidence_refs=evidence,
        evidence_status=EvidenceClaimStatus.CANDIDATE,
    )


def dependency(
    upstream: UUID,
    *,
    enforcement: DependencyEnforcement = DependencyEnforcement.HARD,
    artifacts: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
) -> ExecutionRequestDependency:
    return ExecutionRequestDependency(
        downstream_request_id=DOWNSTREAM_ID,
        upstream_request_id=upstream,
        enforcement=enforcement,
        required_artifact_refs=artifacts,
        required_evidence_refs=evidence,
    )


def test_informational_dependency_does_not_block_without_an_upstream_fact() -> None:
    decision = ActivationGate.evaluate(
        dependencies=(
            dependency(
                UUID("2a000000-0000-0000-0000-000000000001"),
                enforcement=DependencyEnforcement.INFORMATIONAL,
            ),
        ),
        upstream_facts={},
    )

    assert decision.ready
    assert decision.reason == "ready"


def test_fan_out_returns_ready_for_each_downstream_with_same_facts() -> None:
    upstream_id = UUID("2b000000-0000-0000-0000-000000000001")
    downstream_ids = (
        UUID("2c000000-0000-0000-0000-000000000001"),
        UUID("2c000000-0000-0000-0000-000000000002"),
    )
    facts = {upstream_id: fact(upstream_id)}

    decisions = tuple(
        ActivationGate.evaluate(
            dependencies=(
                ExecutionRequestDependency(
                    downstream_request_id=downstream_id,
                    upstream_request_id=upstream_id,
                    enforcement=DependencyEnforcement.HARD,
                ),
            ),
            upstream_facts=facts,
        )
        for downstream_id in downstream_ids
    )

    assert all(decision.ready for decision in decisions)


def test_fan_in_stays_blocked_until_all_hard_upstreams_succeed() -> None:
    first_id = UUID("2d000000-0000-0000-0000-000000000001")
    second_id = UUID("2d000000-0000-0000-0000-000000000002")
    dependencies = (dependency(first_id), dependency(second_id))

    blocked = ActivationGate.evaluate(
        dependencies=dependencies,
        upstream_facts={first_id: fact(first_id)},
    )
    assert not blocked.ready
    assert blocked.reason == "dependencies_blocked"
    assert blocked.missing_upstream_request_ids == (second_id,)

    ready = ActivationGate.evaluate(
        dependencies=dependencies,
        upstream_facts={first_id: fact(first_id), second_id: fact(second_id)},
    )
    assert ready.ready


def test_release_does_not_override_an_unsatisfied_hard_dependency() -> None:
    upstream_id = UUID("2d000000-0000-0000-0000-000000000003")
    static_decision = ActivationGate.evaluate(
        dependencies=(dependency(upstream_id),),
        upstream_facts={},
    )

    decision = ComposedActivationGate.evaluate(
        plan_transition_allows=True,
        static_decision=static_decision,
        collaboration=CollaborationGateFact.released(run_id=uuid4()),
    )

    assert not decision.ready
    assert decision.reason == "dependencies_blocked"
    assert decision.missing_upstream_request_ids == (upstream_id,)


@pytest.mark.parametrize(
    "status",
    [ExecutionStatus.FAILED, ExecutionStatus.LOST, ExecutionStatus.CANCELED],
)
def test_failed_lost_or_cancelled_upstream_never_satisfies_gate(
    status: ExecutionStatus,
) -> None:
    upstream_id = UUID("2e000000-0000-0000-0000-000000000001")
    decision = ActivationGate.evaluate(
        dependencies=(dependency(upstream_id),),
        upstream_facts={upstream_id: fact(upstream_id, status=status)},
    )

    assert not decision.ready
    assert decision.blocked_upstream_request_ids == (upstream_id,)


def test_success_without_required_artifact_or_evidence_stays_blocked() -> None:
    upstream_id = UUID("2f000000-0000-0000-0000-000000000001")
    decision = ActivationGate.evaluate(
        dependencies=(
            dependency(
                upstream_id,
                artifacts=("artifact_type:diff",),
                evidence=("acceptance:tests-pass",),
            ),
        ),
        upstream_facts={upstream_id: fact(upstream_id)},
    )

    assert not decision.ready
    assert decision.missing_artifact_refs == ("artifact_type:diff",)
    assert decision.missing_evidence_refs == ("acceptance:tests-pass",)


def test_candidate_evidence_satisfies_gate_but_accepted_is_not_a_supported_status() -> None:
    upstream_id = UUID("30000000-0000-0000-0000-000000000001")
    decision = ActivationGate.evaluate(
        dependencies=(dependency(upstream_id, evidence=("acceptance:tests-pass",)),),
        upstream_facts={
            upstream_id: fact(
                upstream_id,
                evidence=("acceptance:tests-pass",),
            ),
        },
    )
    assert decision.ready

    with pytest.raises(ValidationError):
        UpstreamActivationFact(
            request_id=upstream_id,
            execution_status=ExecutionStatus.SUCCEEDED,
            execution_lineage_id=uuid4(),
            evidence_status="accepted",
        )


def test_gate_does_not_change_task_or_subtask_status() -> None:
    upstream_id = UUID("31000000-0000-0000-0000-000000000001")
    decision = ActivationGate.evaluate(
        dependencies=(dependency(upstream_id),),
        upstream_facts={upstream_id: fact(upstream_id)},
    )

    assert decision.ready
    assert not hasattr(decision, "task_status")
    assert not hasattr(decision, "subtask_status")
