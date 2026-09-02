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
from sidera_control_plane.domain.execution_requests import CollaborationGateMode
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
