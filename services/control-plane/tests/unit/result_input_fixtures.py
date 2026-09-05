from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

if TYPE_CHECKING:
    from sidera_control_plane.domain.result_inputs import ResultInputSource


def _selection_hash_payload(value: dict[str, object]) -> dict[str, object]:
    """Build the fixture's independent semantic selection payload."""

    ignored = {
        "created_at",
        "decision_id",
        "id",
        "intent_id",
        "projection_hash",
        "projection_snapshot_id",
        "selection_hash",
    }

    def normalize(item: object) -> object:
        if isinstance(item, dict):
            return {
                key: normalize(item[key]) for key in sorted(item) if key not in ignored
            }
        if isinstance(item, list):
            return [normalize(entry) for entry in item]
        return item

    normalized = normalize(value)
    assert isinstance(normalized, dict)
    return normalized


def _build_selection_mapping() -> dict[str, object]:

    report_a = b"report-a\n"
    report_b = b"report-b\n"
    raw: dict[str, object] = {
        "schema_version": 1,
        "id": "61000000-0000-0000-0000-000000000001",
        "workspace_id": "61000000-0000-0000-0000-000000000002",
        "root_task_id": "61000000-0000-0000-0000-000000000003",
        "run_id": "61000000-0000-0000-0000-000000000004",
        "plan_snapshot_id": "61000000-0000-0000-0000-000000000005",
        "plan_generation": 4,
        "plan_content_hash": "a" * 64,
        "target_worker_item_id": "61000000-0000-0000-0000-000000000006",
        "target_execution_request_id": "61000000-0000-0000-0000-000000000007",
        "target_request_version": 3,
        "projection_snapshot_id": "61000000-0000-0000-0000-000000000008",
        "projection_hash": "b" * 64,
        "decision_id": "61000000-0000-0000-0000-000000000009",
        "intent_id": "61000000-0000-0000-0000-000000000010",
        "groups": [
            {
                "group_key": "research",
                "rule_hash": "c" * 64,
                "sources": [
                    {
                        "worker_item_key": "a",
                        "worker_item_id": "61000000-0000-0000-0000-000000000011",
                        "subtask_path": ["research"],
                        "effective_request_id": "61000000-0000-0000-0000-000000000012",
                        "allowed_lineage_ids": ["61000000-0000-0000-0000-000000000013"],
                        "effective_lineage_id": "61000000-0000-0000-0000-000000000013",
                        "effective_execution_id": "61000000-0000-0000-0000-000000000014",
                        "attempt": 1,
                        "inheritance_refs": [],
                        "priority": 1,
                        "source_basis_hash": "d" * 64,
                        "artifacts": [
                            {
                                "artifact_id": "61000000-0000-0000-0000-000000000015",
                                "artifact_type": "report",
                                "content_hash": sha256(report_a).hexdigest(),
                                "size_bytes": len(report_a),
                                "media_type": "text/plain",
                            }
                        ],
                        "evidence": [
                            {
                                "evidence_id": "61000000-0000-0000-0000-000000000016",
                                "acceptance_criterion_ref": "acceptance:research-complete",
                                "artifact_ids": [
                                    "61000000-0000-0000-0000-000000000015"
                                ],
                            }
                        ],
                    },
                    {
                        "worker_item_key": "b",
                        "worker_item_id": "61000000-0000-0000-0000-000000000017",
                        "subtask_path": ["research"],
                        "effective_request_id": "61000000-0000-0000-0000-000000000018",
                        "allowed_lineage_ids": ["61000000-0000-0000-0000-000000000019"],
                        "effective_lineage_id": "61000000-0000-0000-0000-000000000019",
                        "effective_execution_id": "61000000-0000-0000-0000-000000000020",
                        "attempt": 1,
                        "inheritance_refs": [],
                        "priority": 2,
                        "source_basis_hash": "e" * 64,
                        "artifacts": [
                            {
                                "artifact_id": "61000000-0000-0000-0000-000000000021",
                                "artifact_type": "report",
                                "content_hash": sha256(report_b).hexdigest(),
                                "size_bytes": len(report_b),
                                "media_type": "text/plain",
                            }
                        ],
                        "evidence": [
                            {
                                "evidence_id": "61000000-0000-0000-0000-000000000022",
                                "acceptance_criterion_ref": "acceptance:research-complete",
                                "artifact_ids": [
                                    "61000000-0000-0000-0000-000000000021"
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
        "hard_inputs": [],
        "created_at": "2026-08-25T12:00:00+00:00",
    }
    payload = _selection_hash_payload(raw)
    raw["selection_hash"] = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return raw


_SELECTION_MAPPING = _build_selection_mapping()


def selection_mapping() -> dict[str, object]:
    """Return an independent, valid immutable-selection fixture."""

    return deepcopy(_SELECTION_MAPPING)


def effective_source_facts(*, request_case: str):
    from sidera_control_plane.application.collaboration_projection import (
        CollaborationArtifactFact,
        CollaborationEvidenceFact,
        CollaborationExecutionBindingFact,
        CollaborationExecutionFact,
        CollaborationFacts,
        CollaborationHandoffTransitionFact,
        CollaborationPlanHeadFact,
        CollaborationRequestFact,
        CollaborationWorkerPlacementFact,
    )
    from sidera_control_plane.domain.modules import (
        CollaborationRuntimeCapabilityProfile,
        IdempotencyCapability,
        StabilityLevel,
    )
    from sidera_control_plane.domain.plans import TaskPlanSnapshot

    allowed_cases = {
        "current",
        "authorized_continuation",
        "authorized_handoff",
        "unrelated_newer_request",
        "ambiguous_current",
    }
    if request_case not in allowed_cases:
        raise ValueError(f"unsupported effective source case: {request_case}")

    def identity(name: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"sidera:r6.3.4:source:{name}")

    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    workspace_id = identity("workspace")
    root_task_id = identity("root")
    plan_snapshot_id = identity("plan")
    run_id = identity("run")
    worker_item_id = identity("worker-a")
    unrelated_worker_item_id = identity("worker-unrelated")
    subtask_id = identity("subtask")
    base_request_id = identity("request-base")
    target_request_id = identity("request-target")
    base_execution_id = identity("execution-base")
    continuation_execution_id = identity("execution-continuation")
    target_execution_id = identity("execution-target")
    handoff_id = identity("handoff")

    capability = CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime.v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.STABLE,
    )
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=plan_snapshot_id,
        schema_version=3,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="result-input-source-fixture",
        title="Result source plan",
        goal_summary="Resolve exact result source identities",
        acceptance_refs=("acceptance:research-complete",),
        published_by="test",
        published_at=now,
    )

    def request(
        request_id: UUID,
        *,
        item_id: UUID = worker_item_id,
        lineage_id: UUID = base_execution_id,
        handoff_transition_id: UUID | None = None,
    ) -> CollaborationRequestFact:
        return CollaborationRequestFact(
            id=request_id,
            subtask_key="research",
            worker_item_key="a",
            subtask_task_id=subtask_id,
            worker_item_id=item_id,
            version=1,
            status="materialized",
            collaboration_gate_mode="unmanaged",
            gate_reason="ready",
            execution_lineage_id=lineage_id,
            first_execution_id=lineage_id,
            expected_artifact_refs=("artifact_type:report",),
            expected_evidence_refs=("acceptance:research-complete",),
            max_attempts=2,
            handoff_transition_id=handoff_transition_id,
        )

    def execution(
        execution_id: UUID,
        *,
        item_id: UUID = worker_item_id,
        source_execution_id: UUID | None = None,
        handoff_transition_id: UUID | None = None,
    ) -> CollaborationExecutionFact:
        return CollaborationExecutionFact(
            id=execution_id,
            lineage_id=execution_id,
            task_id=subtask_id,
            worker_item_id=item_id,
            attempt=1,
            status="succeeded",
            failure_code=None,
            started_at=now,
            finished_at=now,
            version=1,
            max_attempts=2,
            source_execution_id=source_execution_id,
            handoff_transition_id=handoff_transition_id,
        )

    requests = [request(base_request_id)]
    executions = [execution(base_execution_id)]
    transitions = []
    if request_case == "authorized_continuation":
        executions.append(
            execution(
                continuation_execution_id,
                source_execution_id=base_execution_id,
            )
        )
    elif request_case == "authorized_handoff":
        requests.append(
            request(
                target_request_id,
                lineage_id=target_execution_id,
                handoff_transition_id=handoff_id,
            )
        )
        executions.append(
            execution(
                target_execution_id,
                source_execution_id=base_execution_id,
                handoff_transition_id=handoff_id,
            )
        )
        transitions.append(
            CollaborationHandoffTransitionFact(
                id=handoff_id,
                worker_item_id=worker_item_id,
                source_execution_id=base_execution_id,
                target_execution_id=target_execution_id,
                status="converged",
                phase="complete",
                writer_state="target_active",
            )
        )
    elif request_case == "unrelated_newer_request":
        unrelated_execution_id = identity("execution-unrelated")
        requests.append(
            request(
                identity("request-unrelated"),
                item_id=unrelated_worker_item_id,
                lineage_id=unrelated_execution_id,
            )
        )
        executions.append(
            execution(
                unrelated_execution_id,
                item_id=unrelated_worker_item_id,
            )
        )
    elif request_case == "ambiguous_current":
        duplicate_execution_id = identity("execution-duplicate")
        requests.append(
            request(identity("request-duplicate"), lineage_id=duplicate_execution_id)
        )
        executions.append(execution(duplicate_execution_id))

    artifacts = tuple(
        CollaborationArtifactFact(
            id=identity(f"artifact-{item.id}"),
            execution_id=item.id,
            task_id=subtask_id,
            artifact_type="report",
            content_hash=sha256(f"report:{item.id}".encode()).hexdigest(),
            size_bytes=64,
            media_type="text/plain",
        )
        for item in executions
    )
    evidence = tuple(
        CollaborationEvidenceFact(
            id=identity(f"evidence-{item.id}"),
            execution_id=item.id,
            task_id=subtask_id,
            acceptance_criterion_ref="acceptance:research-complete",
            status="candidate",
            artifact_ids=(artifacts[index].id,),
        )
        for index, item in enumerate(executions)
    )
    return CollaborationFacts(
        plan_head=CollaborationPlanHeadFact(
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            run_id=run_id,
            plan_snapshot_id=plan_snapshot_id,
            plan_generation=1,
            plan_content_hash="a" * 64,
            run_resource_version=1,
            runtime_capability_snapshot=capability,
        ),
        plan_snapshot=snapshot,
        requests=tuple(requests),
        dependencies=(),
        executions=tuple(executions),
        artifacts=artifacts,
        evidence=evidence,
        releases=(),
        previous_strategy_state={},
        worker_placements=(
            CollaborationWorkerPlacementFact(
                worker_item_key="a",
                worker_item_id=worker_item_id,
                subtask_key="research",
                subtask_task_id=subtask_id,
                subtask_path=("research",),
            ),
        ),
        execution_bindings=tuple(
            CollaborationExecutionBindingFact(
                id=identity(f"binding-{item.id}"),
                execution_id=item.id,
                worker_id=identity(f"worker-{item.id}"),
                version=1,
            )
            for item in executions
        ),
        handoff_transitions=tuple(transitions),
    )


def result_source(
    key: str,
    *,
    execution_status: str = "succeeded",
    attempt: int = 1,
    retry_possible: bool = False,
    outcome_unknown: bool = False,
) -> ResultInputSource:
    """Return a deterministic, post-resolution source for evaluator tests."""

    from sidera_control_plane.domain.artifacts import ArtifactType
    from sidera_control_plane.domain.execution import ExecutionStatus
    from sidera_control_plane.domain.result_inputs import (
        ResultInputArtifact,
        ResultInputEvidence,
        ResultInputSource,
    )

    def identity(kind: str) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"sidera:r6.3.4:evaluation:{key}:{attempt}:{kind}",
        )

    artifact_id = identity("report")
    body = f"report:{key}:{attempt}".encode()
    return ResultInputSource(
        worker_item_key=key,
        worker_item_id=identity("worker-item"),
        subtask_path=("research",),
        effective_request_id=identity("request"),
        allowed_lineage_ids=(identity("lineage"),),
        effective_execution_id=identity("execution"),
        attempt=attempt,
        execution_status=ExecutionStatus(execution_status),
        inheritance_refs=(),
        artifacts=(
            ResultInputArtifact(
                artifact_id=artifact_id,
                artifact_type=ArtifactType.REPORT,
                content_hash=sha256(body).hexdigest(),
                size_bytes=len(body),
                media_type="text/plain",
            ),
        ),
        evidence=(
            ResultInputEvidence(
                evidence_id=identity("evidence"),
                acceptance_criterion_ref="acceptance:research-complete",
                artifact_ids=(artifact_id,),
            ),
        ),
        retry_possible=retry_possible,
        outcome_unknown=outcome_unknown,
        reason_code=None,
        source_basis_hash=None,
    )


def proposal_v5_mapping() -> dict[str, object]:
    """Return a complete initial proposal using the input-group policy contract."""

    from .test_planning_proposals_v4 import proposal_v4

    raw = deepcopy(proposal_v4().to_mapping())
    raw["schema_version"] = 5
    subtasks = raw["subtasks"]
    worker_items = raw["worker_items"]
    milestones = raw["milestones"]
    intents = raw["execution_intents"]
    assert isinstance(subtasks, list)
    assert isinstance(worker_items, list)
    assert isinstance(milestones, list)
    assert isinstance(intents, list)
    subtasks.append(
        {
            "key": "review",
            "kind": "child",
            "title": "Review",
            "goal_summary": "Review the implementation.",
            "acceptance_refs": ["acceptance:review"],
            "expected_outputs": ["artifact_type:report"],
            "milestone_key": "m2",
            "source_refs": ["brief:1"],
        }
    )
    worker_items.append(
        {
            "key": "review-worker",
            "subtask_key": "review",
            "title": "Review worker",
            "role": "reviewer",
            "responsibility": "Review the implementation.",
            "capability_requirements": ["code"],
            "worker_policy_ref": None,
            "configuration_ref": None,
            "concurrency_limit": 1,
            "source_refs": ["brief:1"],
            "pinned_worker_id": None,
        }
    )
    milestones.append(
        {
            "key": "m2",
            "title": "Review",
            "subtask_keys": ["review"],
            "acceptance_refs": ["acceptance:review"],
            "order_hint": 2,
        }
    )
    intents.append(
        {
            "worker_item_key": "review-worker",
            "purpose": "Review the implementation.",
            "context_refs": ["context:brief"],
            "expected_artifact_refs": ["artifact_type:report"],
            "required_evidence_refs": ["acceptance:review"],
            "capability_requirements": ["code"],
            "adapter_preferences": ["opencode"],
            "budget_class": "standard",
            "deadline_seconds": 60,
            "not_before_seconds": None,
            "max_attempts": 1,
            "activation_dependencies": [],
        }
    )
    raw["collaboration_policy_request"] = {
        "schema_version": 2,
        "mode": "enabled",
        "default_policy": {"strategy_id": "all_required"},
        "subtask_overrides": [],
        "input_groups": [
            {
                "group_key": "implementation",
                "title": "Implementation result",
                "consumer_worker_item_key": "review-worker",
                "candidates": [{"worker_item_key": "build-worker", "priority": 1}],
                "requirements": {
                    "execution_status": "succeeded",
                    "required_artifact_refs": ["artifact_type:build"],
                    "required_evidence_refs": ["acceptance:build"],
                    "evidence_status": "candidate",
                },
                "mode": "all",
                "minimum_count": None,
                "selection_strategy": "plan_order",
            }
        ],
    }
    return raw


def revision_v5_mapping() -> dict[str, object]:
    """Return a complete target revision with an explicit result-input group."""

    from .test_planning_revision_proposals import proposal

    raw = deepcopy(proposal().to_mapping())
    raw["schema_version"] = 5

    workers = raw["worker_items"]
    intents = raw["execution_intents"]
    worker_decisions = raw["worker_item_identity_decisions"]
    assert isinstance(workers, list)
    assert isinstance(intents, list)
    assert isinstance(worker_decisions, list)

    workers[0]["key"] = "a"
    intents[0]["worker_item_key"] = "a"
    worker_decisions[0]["target_worker_item_key"] = "a"

    for key, title in (
        ("b", "Worker B"),
        ("c", "Worker C"),
        ("summary", "Summary worker"),
    ):
        worker = deepcopy(workers[0])
        worker["key"] = key
        worker["title"] = title
        worker["role"] = "summarizer" if key == "summary" else "builder"
        worker["responsibility"] = (
            "Summarize the selected implementation results."
            if key == "summary"
            else f"Produce implementation result {key}."
        )
        worker["pinned_worker_id"] = None
        workers.append(worker)

        intent = deepcopy(intents[0])
        intent["worker_item_key"] = key
        intent["purpose"] = worker["responsibility"]
        intents.append(intent)

        decision = deepcopy(worker_decisions[0])
        decision["target_worker_item_key"] = key
        decision["strategy"] = "create"
        decision["existing_worker_item_id"] = None
        decision["derived_from_worker_item_ids"] = []
        decision["context_disposition"] = "unchanged"
        decision["change_reasons"] = ["new target WorkerItem"]
        worker_decisions.append(decision)

    raw["collaboration_policy_request"] = {
        "schema_version": 2,
        "mode": "enabled",
        "default_policy": {"strategy_id": "all_required"},
        "subtask_overrides": [],
        "input_groups": [
            {
                "group_key": "research",
                "title": "Research results",
                "consumer_worker_item_key": "summary",
                "candidates": [
                    {"worker_item_key": "a", "priority": 1},
                    {"worker_item_key": "b", "priority": 2},
                    {"worker_item_key": "c", "priority": 3},
                ],
                "requirements": {
                    "execution_status": "succeeded",
                    "required_artifact_refs": ["artifact_type:build"],
                    "required_evidence_refs": ["acceptance:implementation"],
                    "evidence_status": "candidate",
                },
                "mode": "at_least",
                "minimum_count": 2,
                "selection_strategy": "plan_order",
            }
        ],
    }
    return raw


def group_mapping() -> dict[str, object]:
    """Return a fresh, valid at-least-K input group mapping for unit tests."""

    return deepcopy(
        {
            "group_key": "research",
            "title": "Research reports",
            "consumer_worker_item_key": "summary",
            "candidates": [
                {"worker_item_key": "a", "priority": 1},
                {"worker_item_key": "b", "priority": 2},
                {"worker_item_key": "c", "priority": 3},
            ],
            "requirements": {
                "execution_status": "succeeded",
                "required_artifact_refs": ["artifact_type:report"],
                "required_evidence_refs": ["acceptance:research-complete"],
                "evidence_status": "candidate",
            },
            "mode": "at_least",
            "minimum_count": 2,
            "selection_strategy": "plan_order",
        }
    )
