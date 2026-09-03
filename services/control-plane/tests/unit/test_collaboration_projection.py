from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.application.collaboration_projection import (
    CollaborationArtifactFact,
    CollaborationEvidenceFact,
    CollaborationExecutionFact,
    CollaborationFacts,
    CollaborationPlanHeadFact,
    CollaborationProjectionAssembler,
    CollaborationRequestFact,
    ProjectionTooLarge,
)
from sidera_control_plane.application.ports import CollaborationFactStorePort
from sidera_control_plane.domain.execution_requests import (
    CollaborationGateMode,
    DependencyEnforcement,
    ExecutionRequestDependency,
)
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot

WORKSPACE_ID = UUID("21000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
PLAN_SNAPSHOT_ID = UUID("32000000-0000-0000-0000-000000000001")
RUN_ID = UUID("33000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("34000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("35000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("36000000-0000-0000-0000-000000000001")
LINEAGE_ID = UUID("37000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("38000000-0000-0000-0000-000000000001")
ARTIFACT_ID = UUID("39000000-0000-0000-0000-000000000001")
EVIDENCE_ID = UUID("3a000000-0000-0000-0000-000000000001")
UPSTREAM_REQUEST_ID = UUID("3b000000-0000-0000-0000-000000000001")
DOWNSTREAM_REQUEST_ID = UUID("3c000000-0000-0000-0000-000000000001")
UPSTREAM_SUBTASK_ID = UUID("3d000000-0000-0000-0000-000000000001")
DOWNSTREAM_SUBTASK_ID = UUID("3e000000-0000-0000-0000-000000000001")
UPSTREAM_WORKER_ITEM_ID = UUID("3f000000-0000-0000-0000-000000000001")
DOWNSTREAM_WORKER_ITEM_ID = UUID("40000000-0000-0000-0000-000000000001")
UPSTREAM_LINEAGE_ID = UUID("41000000-0000-0000-0000-000000000001")
UPSTREAM_EXECUTION_ID = UUID("42000000-0000-0000-0000-000000000001")
UPSTREAM_ARTIFACT_ID = UUID("43000000-0000-0000-0000-000000000001")
UPSTREAM_EVIDENCE_ID = UUID("44000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _capability(
    *, max_projection_bytes: int = 262_144
) -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime.v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=max_projection_bytes,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.STABLE,
    )


def _facts() -> CollaborationFacts:
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=PLAN_SNAPSHOT_ID,
        schema_version=3,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=7,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="projection-test",
        title="Runtime projection",
        goal_summary="Assemble a bounded runtime view",
        acceptance_refs=("acceptance.runtime",),
        published_by="test",
        published_at=NOW,
    )
    return CollaborationFacts(
        plan_head=CollaborationPlanHeadFact(
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            run_id=RUN_ID,
            plan_snapshot_id=PLAN_SNAPSHOT_ID,
            plan_generation=7,
            plan_content_hash="a" * 64,
            run_resource_version=3,
            runtime_capability_snapshot=_capability(),
        ),
        plan_snapshot=snapshot,
        requests=(
            CollaborationRequestFact(
                id=REQUEST_ID,
                subtask_key="subtask-a",
                worker_item_key="worker-a",
                subtask_task_id=SUBTASK_ID,
                worker_item_id=WORKER_ITEM_ID,
                version=2,
                status="blocked",
                collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED.value,
                gate_reason="awaiting_runtime",
                execution_lineage_id=LINEAGE_ID,
                first_execution_id=EXECUTION_ID,
                expected_artifact_refs=("artifact.output",),
                expected_evidence_refs=("evidence.output",),
            ),
        ),
        dependencies=(),
        executions=(
            CollaborationExecutionFact(
                id=EXECUTION_ID,
                lineage_id=LINEAGE_ID,
                task_id=SUBTASK_ID,
                worker_item_id=WORKER_ITEM_ID,
                attempt=1,
                status="finished",
                failure_code=None,
                started_at=NOW,
                finished_at=NOW,
                version=4,
            ),
        ),
        artifacts=(
            CollaborationArtifactFact(
                id=ARTIFACT_ID,
                execution_id=EXECUTION_ID,
                task_id=SUBTASK_ID,
                artifact_type="report",
                content_hash="b" * 64,
                size_bytes=128,
                media_type="text/plain",
            ),
        ),
        evidence=(
            CollaborationEvidenceFact(
                id=EVIDENCE_ID,
                execution_id=EXECUTION_ID,
                task_id=SUBTASK_ID,
                acceptance_criterion_ref="evidence.output",
                status="candidate",
                artifact_ids=(ARTIFACT_ID,),
            ),
        ),
        releases=(),
        previous_strategy_state={
            "safe_counter": 2,
            "provider_key": "provider-key",
            "trace": "trace body",
            "knowledge_body": "knowledge body",
        },
    )


def test_assembler_uses_authoritative_facts_and_excludes_private_bodies() -> None:
    projection = CollaborationProjectionAssembler().assemble(_facts(), fact_cursor=42)
    encoded = projection.canonical_bytes()

    assert b"provider-key" not in encoded
    assert b"trace body" not in encoded
    assert b"knowledge body" not in encoded
    assert projection.fact_cursor == 42
    assert projection.root_task_id == ROOT_TASK_ID
    assert projection.execution_requests[0]["id"] == str(REQUEST_ID)
    assert projection.execution_requests[0]["eligible"] is False


def test_assembler_marks_an_active_entry_request_eligible() -> None:
    facts = _facts()
    enabled_head = replace(
        facts.plan_head,
        policy_snapshot={
            "schema_version": 1,
            "default_policy": {"enabled": True, "strategy_id": "all_required"},
            "subtask_overrides": [],
        },
    )
    enabled = replace(facts, plan_head=enabled_head)

    projection = CollaborationProjectionAssembler().assemble(enabled, fact_cursor=42)

    assert projection.execution_requests[0]["eligible"] is True
    assert projection.execution_requests[0]["eligibility_reason"] == "eligible"


def test_assembler_applies_hard_artifact_and_evidence_join_rules() -> None:
    facts = _facts()
    upstream = replace(
        facts.requests[0],
        id=UPSTREAM_REQUEST_ID,
        subtask_key="subtask-upstream",
        worker_item_key="worker-upstream",
        subtask_task_id=UPSTREAM_SUBTASK_ID,
        worker_item_id=UPSTREAM_WORKER_ITEM_ID,
        status="materialized",
        execution_lineage_id=UPSTREAM_LINEAGE_ID,
        first_execution_id=UPSTREAM_EXECUTION_ID,
    )
    downstream = replace(
        facts.requests[0],
        id=DOWNSTREAM_REQUEST_ID,
        subtask_key="subtask-downstream",
        worker_item_key="worker-downstream",
        subtask_task_id=DOWNSTREAM_SUBTASK_ID,
        worker_item_id=DOWNSTREAM_WORKER_ITEM_ID,
        execution_lineage_id=None,
        first_execution_id=None,
    )
    dependency = ExecutionRequestDependency(
        downstream_request_id=DOWNSTREAM_REQUEST_ID,
        upstream_request_id=UPSTREAM_REQUEST_ID,
        enforcement=DependencyEnforcement.HARD,
        required_artifact_refs=("artifact_type:report",),
        required_evidence_refs=("acceptance:verify",),
    )
    enabled_head = replace(
        facts.plan_head,
        policy_snapshot={
            "schema_version": 1,
            "default_policy": {"enabled": True, "strategy_id": "all_required"},
            "subtask_overrides": [],
        },
    )
    joined = replace(
        facts,
        plan_head=enabled_head,
        requests=(upstream, downstream),
        dependencies=(dependency,),
        executions=(
            replace(
                facts.executions[0],
                id=UPSTREAM_EXECUTION_ID,
                lineage_id=UPSTREAM_LINEAGE_ID,
                task_id=UPSTREAM_SUBTASK_ID,
                worker_item_id=UPSTREAM_WORKER_ITEM_ID,
                status="succeeded",
            ),
        ),
        artifacts=(
            replace(
                facts.artifacts[0],
                id=UPSTREAM_ARTIFACT_ID,
                execution_id=UPSTREAM_EXECUTION_ID,
                task_id=UPSTREAM_SUBTASK_ID,
                artifact_type="report",
            ),
        ),
        evidence=(
            replace(
                facts.evidence[0],
                id=UPSTREAM_EVIDENCE_ID,
                execution_id=UPSTREAM_EXECUTION_ID,
                task_id=UPSTREAM_SUBTASK_ID,
                acceptance_criterion_ref="acceptance:verify",
            ),
        ),
    )

    projection = CollaborationProjectionAssembler().assemble(joined, fact_cursor=42)
    request_by_id = {item["id"]: item for item in projection.execution_requests}

    assert request_by_id[str(DOWNSTREAM_REQUEST_ID)]["eligible"] is True
    assert request_by_id[str(DOWNSTREAM_REQUEST_ID)]["eligibility_reason"] == "eligible"

    blocked_upstream = replace(
        joined,
        executions=(replace(joined.executions[0], status="running"),),
    )
    blocked_projection = CollaborationProjectionAssembler().assemble(
        blocked_upstream, fact_cursor=43
    )
    blocked_by_id = {item["id"]: item for item in blocked_projection.execution_requests}
    assert blocked_by_id[str(DOWNSTREAM_REQUEST_ID)]["eligible"] is False
    assert (
        blocked_by_id[str(DOWNSTREAM_REQUEST_ID)]["eligibility_reason"]
        == "dependencies_blocked"
    )

    missing_artifact = replace(joined, artifacts=())
    missing_artifact_projection = CollaborationProjectionAssembler().assemble(
        missing_artifact, fact_cursor=44
    )
    assert {
        item["id"]: item["eligible"]
        for item in missing_artifact_projection.execution_requests
    }[str(DOWNSTREAM_REQUEST_ID)] is False

    missing_evidence = replace(joined, evidence=())
    missing_evidence_projection = CollaborationProjectionAssembler().assemble(
        missing_evidence, fact_cursor=45
    )
    assert {
        item["id"]: item["eligible"]
        for item in missing_evidence_projection.execution_requests
    }[str(DOWNSTREAM_REQUEST_ID)] is False


def test_assembler_normalizes_bare_acceptance_evidence_refs() -> None:
    facts = _facts()
    upstream = replace(
        facts.requests[0],
        id=UPSTREAM_REQUEST_ID,
        subtask_key="subtask-upstream",
        worker_item_key="worker-upstream",
        subtask_task_id=UPSTREAM_SUBTASK_ID,
        worker_item_id=UPSTREAM_WORKER_ITEM_ID,
        status="materialized",
        execution_lineage_id=UPSTREAM_LINEAGE_ID,
        first_execution_id=UPSTREAM_EXECUTION_ID,
    )
    downstream = replace(
        facts.requests[0],
        id=DOWNSTREAM_REQUEST_ID,
        subtask_key="subtask-downstream",
        worker_item_key="worker-downstream",
        subtask_task_id=DOWNSTREAM_SUBTASK_ID,
        worker_item_id=DOWNSTREAM_WORKER_ITEM_ID,
        execution_lineage_id=None,
        first_execution_id=None,
    )
    joined = replace(
        facts,
        plan_head=replace(
            facts.plan_head,
            policy_snapshot={
                "schema_version": 1,
                "default_policy": {"enabled": True, "strategy_id": "all_required"},
                "subtask_overrides": [],
            },
        ),
        requests=(upstream, downstream),
        dependencies=(
            ExecutionRequestDependency(
                downstream_request_id=DOWNSTREAM_REQUEST_ID,
                upstream_request_id=UPSTREAM_REQUEST_ID,
                enforcement=DependencyEnforcement.HARD,
                required_artifact_refs=("artifact_type:report",),
                required_evidence_refs=("acceptance:verify",),
            ),
        ),
        executions=(
            replace(
                facts.executions[0],
                id=UPSTREAM_EXECUTION_ID,
                lineage_id=UPSTREAM_LINEAGE_ID,
                task_id=UPSTREAM_SUBTASK_ID,
                worker_item_id=UPSTREAM_WORKER_ITEM_ID,
                status="succeeded",
            ),
        ),
        artifacts=(
            replace(
                facts.artifacts[0],
                id=UPSTREAM_ARTIFACT_ID,
                execution_id=UPSTREAM_EXECUTION_ID,
                task_id=UPSTREAM_SUBTASK_ID,
                artifact_type="report",
            ),
        ),
        evidence=(
            replace(
                facts.evidence[0],
                id=UPSTREAM_EVIDENCE_ID,
                execution_id=UPSTREAM_EXECUTION_ID,
                task_id=UPSTREAM_SUBTASK_ID,
                acceptance_criterion_ref="verify",
            ),
        ),
    )

    projection = CollaborationProjectionAssembler().assemble(joined, fact_cursor=46)

    assert {
        item["id"]: item["eligible"] for item in projection.execution_requests
    }[str(DOWNSTREAM_REQUEST_ID)] is True


def test_assembler_does_not_block_on_informational_dependency() -> None:
    facts = _facts()
    informational = ExecutionRequestDependency(
        downstream_request_id=REQUEST_ID,
        upstream_request_id=UUID("45000000-0000-0000-0000-000000000001"),
        enforcement=DependencyEnforcement.INFORMATIONAL,
    )
    enabled = replace(
        facts,
        plan_head=replace(
            facts.plan_head,
            policy_snapshot={
                "schema_version": 1,
                "default_policy": {"enabled": True, "strategy_id": "all_required"},
                "subtask_overrides": [],
            },
        ),
        dependencies=(informational,),
    )

    projection = CollaborationProjectionAssembler().assemble(enabled, fact_cursor=42)

    assert projection.execution_requests[0]["eligible"] is True


def test_assembler_keeps_release_requests_ineligible_until_run_is_active() -> None:
    facts = _facts()
    pending = replace(
        facts,
        plan_head=replace(
            facts.plan_head,
            run_status="pending",
            policy_snapshot={
                "schema_version": 1,
                "default_policy": {"enabled": True, "strategy_id": "all_required"},
                "subtask_overrides": [],
            },
        ),
    )

    projection = CollaborationProjectionAssembler().assemble(pending, fact_cursor=42)

    assert projection.execution_requests[0]["eligible"] is False
    assert (
        projection.execution_requests[0]["eligibility_reason"]
        == "collaboration_run_pending"
    )


def test_assembler_is_stable_and_semantic_changes_change_the_fingerprint() -> None:
    facts = _facts()
    first = CollaborationProjectionAssembler().assemble(facts, fact_cursor=42)
    second = CollaborationProjectionAssembler().assemble(facts, fact_cursor=42)

    assert first.content_hash == second.content_hash
    assert (
        first.fingerprints["semantic_change_hash"]
        == second.fingerprints["semantic_change_hash"]
    )

    changed_request = replace(facts.requests[0], version=facts.requests[0].version + 1)
    changed = CollaborationFacts(
        plan_head=facts.plan_head,
        plan_snapshot=facts.plan_snapshot,
        requests=(changed_request,),
        dependencies=facts.dependencies,
        executions=facts.executions,
        artifacts=facts.artifacts,
        evidence=facts.evidence,
        releases=facts.releases,
        previous_strategy_state=facts.previous_strategy_state,
    )

    assert (
        CollaborationProjectionAssembler()
        .assemble(changed, fact_cursor=43)
        .fingerprints["semantic_change_hash"]
        != first.fingerprints["semantic_change_hash"]
    )


def test_assembler_rejects_oversized_projection_without_truncating() -> None:
    with pytest.raises(ProjectionTooLarge):
        CollaborationProjectionAssembler(max_projection_bytes=1).assemble(
            _facts(), fact_cursor=42
        )


def test_assembler_tolerates_duplicate_source_facts() -> None:
    facts = _facts()
    duplicated = CollaborationFacts(
        plan_head=facts.plan_head,
        plan_snapshot=facts.plan_snapshot,
        requests=facts.requests + facts.requests,
        dependencies=facts.dependencies,
        executions=facts.executions + facts.executions,
        artifacts=facts.artifacts + facts.artifacts,
        evidence=facts.evidence + facts.evidence,
        releases=facts.releases,
        previous_strategy_state=facts.previous_strategy_state,
    )

    projection = CollaborationProjectionAssembler().assemble(duplicated, fact_cursor=42)

    assert len(projection.execution_requests) == 1
    assert len(projection.executions) == 1
    assert len(projection.artifacts) == 1
    assert len(projection.evidence) == 1


def test_fact_store_port_is_explicitly_root_scoped() -> None:
    assert hasattr(CollaborationFactStorePort, "load")
