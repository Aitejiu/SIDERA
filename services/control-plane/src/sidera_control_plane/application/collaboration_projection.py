from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.domain.artifacts import EvidenceClaimStatus
from sidera_control_plane.domain.collaboration import RuntimeRelease
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeProjection,
    RuntimeProjectionV1,
    RuntimeProjectionV2,
    RuntimeResultInputEvaluation,
    canonical_json_bytes,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ActivationGate,
    ExecutionRequestDependency,
    UpstreamActivationFact,
)
from sidera_control_plane.domain.modules import CollaborationRuntimeCapabilityProfile
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.result_inputs import InputGroupV1

GLOBAL_MAX_PROJECTION_BYTES = 1_048_576
_PRIVATE_FACT_KEYS = frozenset(
    {
        "api_key",
        "config",
        "file_content",
        "input",
        "knowledge",
        "knowledge_body",
        "password",
        "provider_api_key",
        "provider_key",
        "raw_input",
        "secret",
        "secret_value",
        "stderr",
        "stdout",
        "trace",
        "trace_body",
        "user_message",
    }
)


class ProjectionTooLarge(ValidationError):
    code = "projection_too_large"

    def __init__(self, *, size_bytes: int, limit_bytes: int) -> None:
        super().__init__(
            "collaboration runtime projection exceeds its byte limit",
            details={"size_bytes": size_bytes, "limit_bytes": limit_bytes},
        )
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


@dataclass(frozen=True, slots=True)
class CollaborationPlanHeadFact:
    workspace_id: UUID
    root_task_id: UUID
    run_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    run_resource_version: int
    runtime_capability_snapshot: CollaborationRuntimeCapabilityProfile
    policy_snapshot: Mapping[str, object] | None = None
    run_status: str = "active"


@dataclass(frozen=True, slots=True)
class CollaborationRequestFact:
    id: UUID
    subtask_key: str
    worker_item_key: str
    subtask_task_id: UUID
    worker_item_id: UUID
    version: int
    status: str
    collaboration_gate_mode: str
    gate_reason: str
    execution_lineage_id: UUID | None
    first_execution_id: UUID | None
    expected_artifact_refs: tuple[str, ...]
    expected_evidence_refs: tuple[str, ...]
    max_attempts: int = 1
    source_execution_id: UUID | None = None
    source_session_id: UUID | None = None
    handoff_transition_id: UUID | None = None
    result_derivation_kind: str | None = None
    source_candidate_id: UUID | None = None
    purpose: str = "execution"


@dataclass(frozen=True, slots=True)
class CollaborationExecutionFact:
    id: UUID
    lineage_id: UUID
    task_id: UUID
    worker_item_id: UUID | None
    attempt: int
    status: str
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    version: int
    max_attempts: int = 1
    source_execution_id: UUID | None = None
    source_session_id: UUID | None = None
    handoff_transition_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CollaborationArtifactFact:
    id: UUID
    execution_id: UUID
    task_id: UUID
    artifact_type: str
    content_hash: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class CollaborationEvidenceFact:
    id: UUID
    execution_id: UUID
    task_id: UUID
    acceptance_criterion_ref: str
    status: str
    artifact_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CollaborationWorkerPlacementFact:
    worker_item_key: str
    worker_item_id: UUID
    subtask_key: str
    subtask_task_id: UUID
    subtask_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CollaborationExecutionBindingFact:
    id: UUID
    execution_id: UUID
    worker_id: UUID
    version: int


@dataclass(frozen=True, slots=True)
class CollaborationHandoffTransitionFact:
    id: UUID
    worker_item_id: UUID
    source_execution_id: UUID
    target_execution_id: UUID | None
    status: str
    phase: str
    writer_state: str


@dataclass(frozen=True, slots=True)
class CollaborationFacts:
    plan_head: CollaborationPlanHeadFact
    plan_snapshot: TaskPlanSnapshot
    requests: tuple[CollaborationRequestFact, ...]
    dependencies: tuple[ExecutionRequestDependency, ...]
    executions: tuple[CollaborationExecutionFact, ...]
    artifacts: tuple[CollaborationArtifactFact, ...]
    evidence: tuple[CollaborationEvidenceFact, ...]
    releases: tuple[RuntimeRelease, ...]
    previous_strategy_state: Mapping[str, object]
    worker_placements: tuple[CollaborationWorkerPlacementFact, ...] = ()
    execution_bindings: tuple[CollaborationExecutionBindingFact, ...] = ()
    handoff_transitions: tuple[CollaborationHandoffTransitionFact, ...] = ()
    result_source_requests: tuple[CollaborationRequestFact, ...] = ()


class CollaborationProjectionAssembler:
    def __init__(
        self,
        *,
        max_projection_bytes: int = GLOBAL_MAX_PROJECTION_BYTES,
    ) -> None:
        if max_projection_bytes < 1:
            raise ValueError("max_projection_bytes must be positive")
        self._max_projection_bytes = max_projection_bytes

    def assemble(
        self,
        facts: CollaborationFacts,
        *,
        fact_cursor: int,
        projection_id: UUID | None = None,
        now: datetime | None = None,
    ) -> RuntimeProjection:
        self._validate_identity(facts, fact_cursor)
        head = facts.plan_head
        capability = head.runtime_capability_snapshot
        policy = self._policy_mapping(facts)
        projection_schema_version = _projection_schema_version(policy)
        if (
            projection_schema_version == 1
            and 1 not in capability.projection_schema_versions
        ):
            raise ValidationError(
                "pinned collaboration runtime does not support projection schema 1"
            )
        if projection_schema_version == 2 and not capability.supports_input_groups_v1():
            raise ValidationError(
                "pinned collaboration runtime is not input-group capable"
            )

        tasks = self._task_mappings(facts.plan_snapshot, head)
        subtasks = self._subtask_mappings(facts.plan_snapshot)
        worker_items = self._worker_item_mappings(facts.plan_snapshot)
        request_facts = _stable_unique(
            facts.requests, key=lambda item: item.id, name="requests"
        )
        dependency_facts = _stable_unique(
            facts.dependencies,
            key=lambda item: (item.downstream_request_id, item.upstream_request_id),
            name="dependencies",
        )
        execution_facts = _stable_unique(
            facts.executions, key=lambda item: item.id, name="executions"
        )
        artifact_facts = _stable_unique(
            facts.artifacts, key=lambda item: item.id, name="artifacts"
        )
        evidence_facts = _stable_unique(
            facts.evidence, key=lambda item: item.id, name="evidence"
        )
        release_facts = _stable_unique(
            facts.releases, key=lambda item: item.id, name="releases"
        )
        placement_facts = _stable_unique(
            facts.worker_placements,
            key=lambda item: (
                item.worker_item_key,
                item.worker_item_id,
                item.subtask_task_id,
            ),
            name="worker placements",
        )
        binding_facts = _stable_unique(
            facts.execution_bindings,
            key=lambda item: item.id,
            name="execution bindings",
        )
        handoff_facts = _stable_unique(
            facts.handoff_transitions,
            key=lambda item: item.id,
            name="handoff transitions",
        )
        result_source_request_facts = _stable_unique(
            facts.result_source_requests,
            key=lambda item: item.id,
            name="result source requests",
        )
        facts_for_dependencies = CollaborationFacts(
            plan_head=facts.plan_head,
            plan_snapshot=facts.plan_snapshot,
            requests=request_facts,
            dependencies=dependency_facts,
            executions=execution_facts,
            artifacts=artifact_facts,
            evidence=evidence_facts,
            releases=release_facts,
            previous_strategy_state=facts.previous_strategy_state,
            worker_placements=placement_facts,
            execution_bindings=binding_facts,
            handoff_transitions=handoff_facts,
            result_source_requests=result_source_request_facts,
        )
        dependencies = self._dependency_mappings(facts_for_dependencies)
        eligibility = self._eligibility_by_request(
            facts_for_dependencies,
            policy=policy,
        )
        requests = tuple(
            self._request_mapping(
                item,
                eligible=eligibility[item.id][0],
                eligibility_reason=eligibility[item.id][1],
            )
            for item in request_facts
        )
        executions = tuple(self._execution_mapping(item) for item in execution_facts)
        artifacts = tuple(self._artifact_mapping(item) for item in artifact_facts)
        evidence = tuple(self._evidence_mapping(item) for item in evidence_facts)
        releases = tuple(item_mapping(item) for item in release_facts)
        previous_strategy_state = cast(
            Mapping[str, object],
            _sanitize_public_json(facts.previous_strategy_state),
        )
        state_size = len(canonical_json_bytes(previous_strategy_state))
        if state_size > capability.max_strategy_state_bytes:
            raise ProjectionTooLarge(
                size_bytes=state_size,
                limit_bytes=capability.max_strategy_state_bytes,
            )
        result_input_evaluations = (
            self._result_input_evaluations(
                facts_for_dependencies,
                policy=policy,
            )
            if projection_schema_version == 2
            else ()
        )

        semantic_payload: dict[str, object] = {
            "policy": policy,
            "tasks": tasks,
            "subtasks": subtasks,
            "worker_items": worker_items,
            "dependencies": dependencies,
            "execution_requests": requests,
            "executions": executions,
            "artifacts": artifacts,
            "evidence": evidence,
            "releases": releases,
            "previous_strategy_state": previous_strategy_state,
        }
        evaluation_mappings = tuple(
            item.to_mapping() for item in result_input_evaluations
        )
        if projection_schema_version == 2:
            semantic_payload["result_input_evaluations"] = evaluation_mappings
        fingerprints = self._fingerprints(
            semantic_payload,
            requests=requests,
            executions=executions,
            artifacts=artifacts,
            evidence=evidence,
            releases=releases,
        )
        if projection_schema_version == 2:
            fingerprints = {
                **fingerprints,
                "result_inputs": _hash_value(evaluation_mappings),
            }
        if projection_id is None:
            projection_id = uuid5(
                NAMESPACE_URL,
                "sidera:collaboration-projection:"
                f"{head.run_id}:{fact_cursor}:{head.run_resource_version}:"
                f"{fingerprints['semantic_change_hash']}",
            )
        created_at = now or datetime.now(UTC)
        if projection_schema_version == 1:
            projection: RuntimeProjection = RuntimeProjectionV1(
                schema_version=1,
                projection_id=projection_id,
                workspace_id=head.workspace_id,
                root_task_id=head.root_task_id,
                run_id=head.run_id,
                plan_snapshot_id=head.plan_snapshot_id,
                plan_generation=head.plan_generation,
                plan_content_hash=head.plan_content_hash,
                run_resource_version=head.run_resource_version,
                fact_cursor=fact_cursor,
                policy=policy,
                tasks=tasks,
                subtasks=subtasks,
                worker_items=worker_items,
                dependencies=dependencies,
                execution_requests=requests,
                executions=executions,
                artifacts=artifacts,
                evidence=evidence,
                releases=releases,
                previous_strategy_state=previous_strategy_state,
                fingerprints=fingerprints,
                created_at=created_at,
            )
        else:
            projection = RuntimeProjectionV2(
                schema_version=2,
                projection_id=projection_id,
                workspace_id=head.workspace_id,
                root_task_id=head.root_task_id,
                run_id=head.run_id,
                plan_snapshot_id=head.plan_snapshot_id,
                plan_generation=head.plan_generation,
                plan_content_hash=head.plan_content_hash,
                run_resource_version=head.run_resource_version,
                fact_cursor=fact_cursor,
                policy=policy,
                tasks=tasks,
                subtasks=subtasks,
                worker_items=worker_items,
                dependencies=dependencies,
                execution_requests=requests,
                executions=executions,
                artifacts=artifacts,
                evidence=evidence,
                releases=releases,
                previous_strategy_state=previous_strategy_state,
                result_input_evaluations=result_input_evaluations,
                fingerprints=fingerprints,
                created_at=created_at,
            )
        size_bytes = len(projection.canonical_bytes())
        limit_bytes = min(self._max_projection_bytes, capability.max_projection_bytes)
        if size_bytes > limit_bytes:
            raise ProjectionTooLarge(
                size_bytes=size_bytes,
                limit_bytes=limit_bytes,
            )
        return projection

    @staticmethod
    def _result_input_evaluations(
        facts: CollaborationFacts,
        *,
        policy: Mapping[str, object],
    ) -> tuple[RuntimeResultInputEvaluation, ...]:
        from sidera_control_plane.application.result_input_evaluation import (
            ResultInputEvaluator,
        )
        from sidera_control_plane.application.result_input_sources import (
            resolve_result_input_source,
        )

        raw_groups = policy.get("input_groups")
        if not isinstance(raw_groups, (list, tuple)):
            raise ValidationError("Projection v2 policy input_groups must be an array")
        groups = tuple(InputGroupV1.from_mapping(item) for item in raw_groups)
        evaluator = ResultInputEvaluator()
        result: list[RuntimeResultInputEvaluation] = []
        for group in groups:
            consumer_request_id = _current_consumer_request_id(group, facts=facts)
            sources = tuple(
                resolve_result_input_source(
                    worker_item_key=candidate.worker_item_key,
                    facts=facts,
                )
                for candidate in group.candidates
            )
            result.append(
                RuntimeResultInputEvaluation(
                    consumer_execution_request_id=consumer_request_id,
                    group_key=group.group_key,
                    evaluation=evaluator.evaluate_group(
                        group=group,
                        sources=sources,
                    ),
                )
            )
        return tuple(result)

    @staticmethod
    def _validate_identity(facts: CollaborationFacts, fact_cursor: int) -> None:
        if fact_cursor < 0:
            raise ValidationError("fact_cursor must be non-negative")
        head = facts.plan_head
        snapshot = facts.plan_snapshot
        if head.workspace_id != snapshot.workspace_id:
            raise ValidationError("projection plan workspace does not match its head")
        if head.root_task_id != snapshot.root_task_id:
            raise ValidationError("projection plan root does not match its head")
        if head.plan_snapshot_id != snapshot.id:
            raise ValidationError("projection plan snapshot does not match its head")
        if head.plan_generation != snapshot.generation:
            raise ValidationError("projection plan generation does not match its head")

    @staticmethod
    def _policy_mapping(facts: CollaborationFacts) -> Mapping[str, object]:
        head_policy = facts.plan_head.policy_snapshot
        if head_policy is not None:
            return cast(Mapping[str, object], _sanitize_public_json(head_policy))
        policy = facts.plan_snapshot.collaboration_policy
        if policy is None:
            return {"mode": "disabled"}
        return cast(Mapping[str, object], _sanitize_public_json(policy.to_mapping()))

    @classmethod
    def _eligibility_by_request(
        cls,
        facts: CollaborationFacts,
        *,
        policy: Mapping[str, object],
    ) -> dict[UUID, tuple[bool, str]]:
        upstream_facts = cls._upstream_activation_facts(facts)
        released_request_ids = {item.execution_request_id for item in facts.releases}
        result: dict[UUID, tuple[bool, str]] = {}
        for request in facts.requests:
            result[request.id] = cls._request_eligibility(
                request,
                facts=facts,
                policy=policy,
                released_request_ids=released_request_ids,
                upstream_facts=upstream_facts,
            )
        return result

    @staticmethod
    def _request_eligibility(
        request: CollaborationRequestFact,
        *,
        facts: CollaborationFacts,
        policy: Mapping[str, object],
        released_request_ids: set[UUID],
        upstream_facts: Mapping[UUID, UpstreamActivationFact],
    ) -> tuple[bool, str]:
        if request.collaboration_gate_mode != "release_required":
            return False, "not_release_required"
        if request.status not in {"blocked", "ready"}:
            return False, "request_closed"
        if facts.plan_head.run_status != "active":
            return False, f"collaboration_run_{facts.plan_head.run_status}"
        if request.id in released_request_ids:
            return False, "already_released"
        enabled, strategy = _effective_policy(policy, request.subtask_key)
        if not enabled:
            return False, "collaboration_policy_disabled"
        if strategy != "all_required":
            return False, "strategy_unsupported"
        decision = ActivationGate.evaluate(
            dependencies=tuple(
                item
                for item in facts.dependencies
                if item.downstream_request_id == request.id
            ),
            upstream_facts=upstream_facts,
        )
        if not decision.ready:
            return False, decision.reason
        return True, "eligible"

    @staticmethod
    def _upstream_activation_facts(
        facts: CollaborationFacts,
    ) -> Mapping[UUID, UpstreamActivationFact]:
        executions_by_lineage: dict[UUID, list[CollaborationExecutionFact]] = {}
        for execution in facts.executions:
            executions_by_lineage.setdefault(execution.lineage_id, []).append(execution)

        artifacts_by_execution: dict[UUID, list[CollaborationArtifactFact]] = {}
        for artifact in facts.artifacts:
            artifacts_by_execution.setdefault(artifact.execution_id, []).append(
                artifact
            )

        evidence_by_execution: dict[UUID, list[CollaborationEvidenceFact]] = {}
        for evidence in facts.evidence:
            evidence_by_execution.setdefault(evidence.execution_id, []).append(evidence)

        result: dict[UUID, UpstreamActivationFact] = {}
        for request in facts.requests:
            lineage_id = request.execution_lineage_id
            matching = (
                sorted(
                    executions_by_lineage.get(lineage_id, ()),
                    key=lambda item: (item.attempt, item.version, str(item.id)),
                )
                if lineage_id is not None
                else []
            )
            latest = matching[-1] if matching else None
            execution_ids = {item.id for item in matching}
            artifact_refs = tuple(
                sorted(
                    {
                        f"artifact_type:{artifact.artifact_type}"
                        for execution_id in execution_ids
                        for artifact in artifacts_by_execution.get(execution_id, ())
                    }
                )
            )
            evidence_refs = tuple(
                sorted(
                    {
                        _normalized_evidence_ref(evidence.acceptance_criterion_ref)
                        for execution_id in execution_ids
                        for evidence in evidence_by_execution.get(execution_id, ())
                        if evidence.status == EvidenceClaimStatus.CANDIDATE.value
                        and _valid_evidence_ref(
                            _normalized_evidence_ref(evidence.acceptance_criterion_ref)
                        )
                    }
                )
            )
            result[request.id] = UpstreamActivationFact(
                request_id=request.id,
                execution_status=_execution_status(latest.status)
                if latest is not None
                else None,
                execution_lineage_id=lineage_id,
                artifact_refs=artifact_refs,
                evidence_refs=evidence_refs,
                evidence_status=EvidenceClaimStatus.CANDIDATE,
            )
        return result

    @staticmethod
    def _task_mappings(
        snapshot: TaskPlanSnapshot,
        head: CollaborationPlanHeadFact,
    ) -> tuple[Mapping[str, object], ...]:
        return (
            {
                "id": str(head.root_task_id),
                "kind": "root",
                "title": snapshot.title,
                "goal_summary": snapshot.goal_summary,
                "acceptance_refs": list(snapshot.acceptance_refs),
                "plan_generation": head.plan_generation,
            },
        )

    @staticmethod
    def _subtask_mappings(
        snapshot: TaskPlanSnapshot,
    ) -> tuple[Mapping[str, object], ...]:
        return tuple(
            {
                "id": str(item.task_id) if item.task_id is not None else None,
                "key": item.key,
                "kind": item.kind.value,
                "title": item.title,
                "goal_summary": item.goal_summary,
                "acceptance_refs": list(item.acceptance_refs),
                "milestone_key": item.milestone_key,
                "expected_outputs": list(item.expected_outputs),
                "source_refs": list(item.source_refs),
            }
            for item in sorted(snapshot.subtasks, key=lambda value: value.key)
        )

    @staticmethod
    def _worker_item_mappings(
        snapshot: TaskPlanSnapshot,
    ) -> tuple[Mapping[str, object], ...]:
        return tuple(
            {
                "id": (
                    str(item.worker_item_id)
                    if item.worker_item_id is not None
                    else None
                ),
                "key": item.key,
                "subtask_key": item.subtask_key,
                "title": item.title,
                "role": item.role,
                "responsibility": item.responsibility,
                "capability_requirements": list(item.capability_requirements),
                "worker_policy_ref": item.worker_policy_ref,
                "configuration_ref": item.configuration_ref,
                "concurrency_limit": item.concurrency_limit,
                "source_refs": list(item.source_refs),
            }
            for item in sorted(snapshot.worker_item_plans, key=lambda value: value.key)
        )

    @staticmethod
    def _dependency_mappings(
        facts: CollaborationFacts,
    ) -> tuple[Mapping[str, object], ...]:
        plan = facts.plan_snapshot
        plan_dependencies: list[Mapping[str, object]] = []
        plan_dependencies.extend(
            {
                "dependency_type": "subtask",
                "upstream_key": item.upstream_subtask_key,
                "downstream_key": item.downstream_subtask_key,
                "kind": item.kind.value,
                "enforcement": item.enforcement.value,
                "required_evidence_refs": list(item.required_evidence_refs),
            }
            for item in plan.subtask_dependencies
        )
        plan_dependencies.extend(
            {
                "dependency_type": "worker_item",
                "upstream_key": item.upstream_worker_item_key,
                "downstream_key": item.downstream_worker_item_key,
                "kind": item.kind,
                "required_artifact_refs": list(item.required_artifact_refs),
            }
            for item in plan.worker_item_dependencies
        )
        plan_dependencies.extend(
            {
                "dependency_type": "execution_request",
                "downstream_request_id": str(item.downstream_request_id),
                "upstream_request_id": str(item.upstream_request_id),
                "enforcement": item.enforcement.value,
                "required_artifact_refs": list(item.required_artifact_refs),
                "required_evidence_refs": list(item.required_evidence_refs),
            }
            for item in facts.dependencies
        )
        return tuple(
            sorted(
                plan_dependencies,
                key=lambda item: canonical_json_bytes(item),
            )
        )

    @staticmethod
    def _request_mapping(
        item: CollaborationRequestFact,
        *,
        eligible: bool,
        eligibility_reason: str,
    ) -> Mapping[str, object]:
        return {
            "id": str(item.id),
            "subtask_key": item.subtask_key,
            "worker_item_key": item.worker_item_key,
            "subtask_task_id": str(item.subtask_task_id),
            "worker_item_id": str(item.worker_item_id),
            "version": item.version,
            "purpose": item.purpose,
            "status": item.status,
            "collaboration_gate_mode": item.collaboration_gate_mode,
            "gate_reason": item.gate_reason,
            "execution_lineage_id": (
                str(item.execution_lineage_id)
                if item.execution_lineage_id is not None
                else None
            ),
            "first_execution_id": (
                str(item.first_execution_id)
                if item.first_execution_id is not None
                else None
            ),
            "result_derivation_kind": item.result_derivation_kind,
            "source_candidate_id": (
                str(item.source_candidate_id)
                if item.source_candidate_id is not None
                else None
            ),
            "expected_artifact_refs": list(item.expected_artifact_refs),
            "expected_evidence_refs": list(item.expected_evidence_refs),
            "eligible": eligible,
            "eligibility_reason": eligibility_reason,
        }

    @staticmethod
    def _execution_mapping(item: CollaborationExecutionFact) -> Mapping[str, object]:
        return {
            "id": str(item.id),
            "lineage_id": str(item.lineage_id),
            "task_id": str(item.task_id),
            "worker_item_id": (
                str(item.worker_item_id) if item.worker_item_id is not None else None
            ),
            "attempt": item.attempt,
            "status": item.status,
            "failure_code": item.failure_code,
            "started_at": _datetime_value(item.started_at),
            "finished_at": _datetime_value(item.finished_at),
            "version": item.version,
        }

    @staticmethod
    def _artifact_mapping(item: CollaborationArtifactFact) -> Mapping[str, object]:
        return {
            "id": str(item.id),
            "execution_id": str(item.execution_id),
            "task_id": str(item.task_id),
            "artifact_type": item.artifact_type,
            "content_hash": item.content_hash,
            "size_bytes": item.size_bytes,
            "media_type": item.media_type,
        }

    @staticmethod
    def _evidence_mapping(item: CollaborationEvidenceFact) -> Mapping[str, object]:
        return {
            "id": str(item.id),
            "execution_id": str(item.execution_id),
            "task_id": str(item.task_id),
            "acceptance_criterion_ref": item.acceptance_criterion_ref,
            "status": item.status,
            "artifact_ids": [str(value) for value in item.artifact_ids],
        }

    @staticmethod
    def _fingerprints(
        semantic_payload: Mapping[str, object],
        *,
        requests: Sequence[Mapping[str, object]],
        executions: Sequence[Mapping[str, object]],
        artifacts: Sequence[Mapping[str, object]],
        evidence: Sequence[Mapping[str, object]],
        releases: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object]:
        return {
            "requests": _hash_value(requests),
            "executions": _hash_value(executions),
            "artifacts": _hash_value(artifacts),
            "evidence": _hash_value(evidence),
            "releases": _hash_value(releases),
            "semantic_change_hash": _hash_value(semantic_payload),
        }


def item_mapping(item: RuntimeRelease) -> Mapping[str, object]:
    return {
        "id": str(item.id),
        "run_id": str(item.run_id),
        "plan_snapshot_id": str(item.plan_snapshot_id),
        "plan_generation": item.plan_generation,
        "plan_content_hash": item.plan_content_hash,
        "decision_id": str(item.decision_id),
        "intent_id": str(item.intent_id),
        "execution_request_id": str(item.execution_request_id),
        "request_version_at_release": item.request_version_at_release,
        "issued_at": _datetime_value(item.issued_at),
    }


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _effective_policy(
    policy: Mapping[str, object], subtask_key: str
) -> tuple[bool, str | None]:
    if policy.get("mode") == "disabled":
        return False, None
    selected: object = policy
    default = policy.get("default_policy")
    if isinstance(default, Mapping):
        selected = default
    overrides = policy.get("subtask_overrides", ())
    if isinstance(overrides, (list, tuple)):
        for item in overrides:
            if isinstance(item, Mapping) and item.get("subtask_key") == subtask_key:
                selected = item
                break
    if not isinstance(selected, Mapping):
        return False, None
    enabled = selected.get("enabled") is True
    strategy = selected.get("strategy_id")
    return enabled, strategy if isinstance(strategy, str) else None


def _projection_schema_version(policy: Mapping[str, object]) -> int:
    value = policy.get("schema_version")
    if value is None:
        return 1
    if type(value) is not int or value not in {1, 2}:
        raise ValidationError("unsupported collaboration policy schema version")
    return value


def _current_consumer_request_id(
    group: InputGroupV1,
    *,
    facts: CollaborationFacts,
) -> UUID:
    placements = tuple(
        item
        for item in facts.worker_placements
        if item.worker_item_key == group.consumer_worker_item_key
    )
    if len(placements) != 1:
        raise ValidationError(
            "input group consumer must have one current WorkerItem placement"
        )
    placement = placements[0]
    requests = tuple(
        item
        for item in facts.requests
        if item.worker_item_key == group.consumer_worker_item_key
        and item.worker_item_id == placement.worker_item_id
        and item.subtask_task_id == placement.subtask_task_id
        and item.handoff_transition_id is None
        and item.purpose != "verification"
        and item.result_derivation_kind != "verification"
    )
    reintegration_requests = tuple(
        item for item in requests if item.purpose == "reintegrate"
    )
    if len(reintegration_requests) == 1:
        return reintegration_requests[0].id
    if len(reintegration_requests) > 1:
        raise ValidationError(
            "input group consumer must have one current reintegration request"
        )
    requests = tuple(item for item in requests if item.purpose != "rework")
    if len(requests) != 1:
        raise ValidationError(
            "input group consumer must have one current execution request"
        )
    return requests[0].id


def _execution_status(value: str) -> ExecutionStatus | None:
    try:
        return ExecutionStatus(value)
    except ValueError:
        return None


def _valid_evidence_ref(value: str) -> bool:
    return (
        value.startswith("acceptance:")
        and len(value) > len("acceptance:")
        and not any(character.isspace() for character in value)
    )


def _normalized_evidence_ref(value: str) -> str:
    return value if value.startswith("acceptance:") else f"acceptance:{value}"


def _hash_value(value: object) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def _stable_unique[FactType, FactKey: Hashable](
    values: Sequence[FactType],
    *,
    key: Callable[[FactType], FactKey],
    name: str,
) -> tuple[FactType, ...]:
    unique: dict[FactKey, FactType] = {}
    for value in values:
        identity = key(value)
        previous = unique.get(identity)
        if previous is not None and previous != value:
            raise ValidationError(f"conflicting duplicate {name} fact")
        unique[identity] = value
    return tuple(unique[identity] for identity in sorted(unique, key=str))


def _sanitize_public_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError("projection mapping keys must be strings")
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _PRIVATE_FACT_KEYS:
                continue
            result[key] = _sanitize_public_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_public_json(item) for item in value]
    return value
