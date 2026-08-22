from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn

from sidera_control_plane.domain.artifacts import ArtifactType
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_profiles import PlanningProfileSnapshot
from sidera_control_plane.domain.planning_proposals import (
    PlanningProposalPayload,
)
from sidera_control_plane.domain.plans import DependencyKind


class ProposalValidationError(ValidationError):
    def __init__(
        self, code: str, detail: str, offending_keys: tuple[str, ...] = ()
    ) -> None:
        ValidationError.__init__(
            self,
            f"{code}: {detail}",
            details={"code": code, "offending_keys": list(offending_keys)},
        )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "offending_keys", offending_keys)


def validate_proposal(
    payload: PlanningProposalPayload | Mapping[str, object],
    *,
    profile_snapshot: PlanningProfileSnapshot,
    brief_revision: int,
) -> PlanningProposalPayload:
    typed_payload = _typed_payload(payload)
    if typed_payload.schema_version != 2:
        _fail("proposal_schema_invalid", "R5.1 requires proposal schema version 2")
    if profile_snapshot.proposal_schema_version != typed_payload.schema_version:
        _fail("proposal_schema_invalid", "proposal schema is not selected by the profile")
    if brief_revision < 1:
        _fail("proposal_schema_invalid", "brief revision must be positive")
    if typed_payload.kind is not PlanningRequestKind.CREATE:
        _fail("proposal_revision_operation_forbidden", "R5.1 accepts create proposals only")
    if typed_payload.base_descriptor is not None:
        _fail("proposal_revision_operation_forbidden", "base descriptor is reserved for revision proposals")
    if typed_payload.impact_policy.revision_operation is not None:
        _fail("proposal_revision_operation_forbidden", "revision operation is not supported")
    if typed_payload.impact_policy.manual_gate:
        _fail("proposal_manual_gate_unsupported", "manual approval gates are not supported in R5.1")

    _validate_unique_keys(typed_payload)
    _validate_references(typed_payload)
    _validate_cycles(typed_payload)
    _validate_capabilities(typed_payload, profile_snapshot)
    _validate_budget(typed_payload, profile_snapshot)
    _validate_intents(typed_payload)
    return typed_payload


def _typed_payload(
    payload: PlanningProposalPayload | Mapping[str, object],
) -> PlanningProposalPayload:
    if isinstance(payload, PlanningProposalPayload):
        return payload
    if not isinstance(payload, Mapping):
        _fail("proposal_schema_invalid", "proposal payload must be a typed mapping")
    if _contains_concrete_identity(
        payload, {"root_task_id", "task_id", "worker_item_id", "pinned_worker_id"}
    ):
        _fail("proposal_worker_binding_forbidden", "proposal cannot contain concrete task or worker identities")
    try:
        return PlanningProposalPayload.from_mapping(payload)
    except ValidationError as exc:
        _fail("proposal_schema_invalid", str(exc))
    raise AssertionError("proposal payload validation did not return")


def _validate_unique_keys(payload: PlanningProposalPayload) -> None:
    _unique(tuple(item.key for item in payload.subtasks), "subtask")
    _unique(tuple(item.key for item in payload.worker_items), "worker item")
    _unique(tuple(item.key for item in payload.subtask_milestones), "milestone")
    intent_keys = tuple(item.worker_item_key for item in payload.execution_intents)
    _unique(intent_keys, "execution intent")
    if not payload.subtasks:
        _fail("proposal_schema_invalid", "proposal must contain at least one subtask")
    if not payload.worker_items:
        _fail("proposal_schema_invalid", "proposal must contain at least one worker item")

    subtask_keys = {item.key for item in payload.subtasks}
    workers_by_subtask: dict[str, int] = {key: 0 for key in subtask_keys}
    for item in payload.worker_items:
        if item.subtask_key not in subtask_keys:
            _fail(
                "proposal_reference_invalid",
                "worker item references an unknown subtask",
                (item.key, item.subtask_key),
            )
        workers_by_subtask[item.subtask_key] += 1
    missing_workers = tuple(key for key, count in workers_by_subtask.items() if count == 0)
    if missing_workers:
        _fail(
            "proposal_reference_invalid",
            "every executable subtask needs a worker item",
            missing_workers,
        )


def _validate_references(payload: PlanningProposalPayload) -> None:
    subtask_keys = {item.key for item in payload.subtasks}
    worker_keys = {item.key for item in payload.worker_items}
    milestone_keys = {item.key for item in payload.subtask_milestones}
    for item in payload.subtasks:
        if item.milestone_key is not None and item.milestone_key not in milestone_keys:
            _fail("proposal_reference_invalid", "subtask milestone reference is unknown", (item.key, item.milestone_key))
        for ref in item.acceptance_refs:
            _validate_evidence_ref(ref, item.key)
    for ref in payload.root_task.acceptance_refs:
        _validate_evidence_ref(ref, "root")
    for milestone in payload.subtask_milestones:
        for key in milestone.subtask_keys:
            if key not in subtask_keys:
                _fail("proposal_reference_invalid", "milestone references an unknown subtask", (milestone.key, key))
        for ref in milestone.acceptance_refs:
            _validate_evidence_ref(ref, milestone.key)
    for dependency in payload.subtask_dependencies:
        _edge_reference(
            dependency.upstream_subtask_key,
            dependency.downstream_subtask_key,
            subtask_keys,
            "subtask dependency",
        )
        for ref in dependency.required_evidence_refs:
            _validate_evidence_ref(ref, dependency.downstream_subtask_key)
    for dependency in payload.worker_item_dependencies:
        _edge_reference(
            dependency.upstream_worker_item_key,
            dependency.downstream_worker_item_key,
            worker_keys,
            "worker item dependency",
        )
        if dependency.kind not in {"hard", "informational"}:
            _fail("proposal_schema_invalid", "worker dependency kind is unsupported", (dependency.kind,))
        for ref in dependency.required_artifact_refs:
            _validate_artifact_ref(ref, dependency.downstream_worker_item_key)
    for intent in payload.execution_intents:
        if intent.worker_item_key not in worker_keys:
            _fail("proposal_intent_mismatch", "execution intent references an unknown worker item", (intent.worker_item_key,))
        for ref in (*intent.expected_artifact_refs,):
            _validate_artifact_ref(ref, intent.worker_item_key)
        for ref in intent.required_evidence_refs:
            _validate_evidence_ref(ref, intent.worker_item_key)
        for dependency in intent.activation_dependencies:
            _edge_reference(
                dependency.upstream_worker_item_key,
                dependency.downstream_worker_item_key,
                worker_keys,
                "activation dependency",
            )
            if dependency.kind not in {"hard", "informational"}:
                _fail("proposal_schema_invalid", "activation dependency kind is unsupported", (dependency.kind,))
            for ref in dependency.required_artifact_refs:
                _validate_artifact_ref(ref, dependency.downstream_worker_item_key)


def _validate_cycles(payload: PlanningProposalPayload) -> None:
    _assert_acyclic(
        tuple(item.key for item in payload.subtasks),
        tuple(
            (item.upstream_subtask_key, item.downstream_subtask_key)
            for item in payload.subtask_dependencies
            if item.kind.value == DependencyKind.HARD.value
        ),
        "subtask hard dependency graph",
    )
    _assert_acyclic(
        tuple(item.key for item in payload.worker_items),
        tuple(
            (item.upstream_worker_item_key, item.downstream_worker_item_key)
            for item in payload.worker_item_dependencies
            if item.kind == DependencyKind.HARD.value
        ),
        "worker item hard dependency graph",
    )
    activation_edges = tuple(
        (dependency.upstream_worker_item_key, dependency.downstream_worker_item_key)
        for intent in payload.execution_intents
        for dependency in intent.activation_dependencies
        if dependency.kind == DependencyKind.HARD.value
    )
    _assert_acyclic(
        tuple(item.key for item in payload.worker_items),
        activation_edges,
        "activation dependency graph",
    )


def _validate_capabilities(
    payload: PlanningProposalPayload,
    profile_snapshot: PlanningProfileSnapshot,
) -> None:
    allowed = set(profile_snapshot.allowed_capabilities)
    forbidden_adapters = set(profile_snapshot.adapter_forbidden)
    for item in payload.worker_items:
        if not set(item.capability_requirements).issubset(allowed):
            _fail("proposal_capability_forbidden", "worker item requests a capability outside the profile", (item.key,))
        if item.pinned_worker_id is not None:
            _fail("proposal_worker_binding_forbidden", "concrete worker binding is not allowed", (item.key,))
    for intent in payload.execution_intents:
        if not set(intent.capability_requirements).issubset(allowed):
            _fail("proposal_capability_forbidden", "execution intent requests a capability outside the profile", (intent.worker_item_key,))
        if set(intent.adapter_preferences).intersection(forbidden_adapters):
            _fail("proposal_capability_forbidden", "execution intent requests a forbidden adapter", (intent.worker_item_key,))


def _validate_budget(
    payload: PlanningProposalPayload,
    profile_snapshot: PlanningProfileSnapshot,
) -> None:
    budget = profile_snapshot.budget
    summary = payload.decision_summary
    if summary.estimated_tokens > budget.max_tokens:
        _fail("proposal_budget_exceeded", "estimated tokens exceed the profile budget")
    if summary.estimated_cost_microunits > budget.max_cost_microunits:
        _fail("proposal_budget_exceeded", "estimated cost exceeds the profile budget")
    if summary.estimated_runtime_seconds > budget.max_runtime_seconds:
        _fail("proposal_budget_exceeded", "estimated runtime exceeds the profile budget")
    if summary.estimated_concurrency > budget.max_concurrency:
        _fail("proposal_budget_exceeded", "estimated concurrency exceeds the profile budget")
    if 1 + len(payload.subtasks) + len(payload.worker_items) > budget.max_nodes:
        _fail("proposal_budget_exceeded", "proposal node count exceeds the profile budget")
    for item in payload.worker_items:
        if item.concurrency_limit is not None and item.concurrency_limit > budget.max_concurrency:
            _fail("proposal_budget_exceeded", "worker item concurrency exceeds the profile budget", (item.key,))
    for intent in payload.execution_intents:
        if intent.max_attempts > profile_snapshot.execution_max_attempts:
            _fail("proposal_budget_exceeded", "execution attempts exceed the profile policy", (intent.worker_item_key,))
        for seconds in (intent.deadline_seconds, intent.not_before_seconds):
            if seconds is not None and seconds > budget.max_runtime_seconds:
                _fail("proposal_budget_exceeded", "execution timing exceeds the profile budget", (intent.worker_item_key,))


def _validate_intents(payload: PlanningProposalPayload) -> None:
    workers = {item.key: item for item in payload.worker_items}
    intent_keys = {item.worker_item_key for item in payload.execution_intents}
    worker_keys = set(workers)
    if intent_keys != worker_keys:
        _fail("proposal_intent_mismatch", "every WorkerItem must have exactly one execution intent")
    for intent in payload.execution_intents:
        worker = workers[intent.worker_item_key]
        if set(intent.capability_requirements) != set(worker.capability_requirements):
            _fail("proposal_intent_mismatch", "intent capabilities do not match the WorkerItem", (intent.worker_item_key,))


def _validate_artifact_ref(value: str, key: str) -> None:
    prefix, separator, artifact_type = value.partition(":")
    if prefix != "artifact_type" or not separator or artifact_type not in {item.value for item in ArtifactType}:
        _fail("proposal_reference_invalid", "artifact reference must use artifact_type:<ArtifactType>", (key,))


def _validate_evidence_ref(value: str, key: str) -> None:
    prefix, separator, criterion = value.partition(":")
    if prefix != "acceptance" or not separator or not criterion.strip():
        _fail("proposal_reference_invalid", "evidence reference must use acceptance:<criterion-ref>", (key,))


def _edge_reference(upstream: str, downstream: str, keys: set[str], label: str) -> None:
    if upstream == downstream:
        _fail("proposal_cycle", f"{label} cannot point to itself", (upstream,))
    if upstream not in keys or downstream not in keys:
        _fail("proposal_reference_invalid", f"{label} references an unknown node", (upstream, downstream))


def _assert_acyclic(keys: tuple[str, ...], edges: tuple[tuple[str, str], ...], label: str) -> None:
    indegree = {key: 0 for key in keys}
    adjacency: dict[str, list[str]] = {key: [] for key in keys}
    for upstream, downstream in edges:
        if upstream not in indegree or downstream not in indegree:
            _fail("proposal_reference_invalid", f"{label} references an unknown node", (upstream, downstream))
        adjacency[upstream].append(downstream)
        indegree[downstream] += 1
    queue = [key for key in keys if indegree[key] == 0]
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for downstream in adjacency[current]:
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                queue.append(downstream)
    if visited != len(keys):
        _fail("proposal_cycle", f"{label} contains a cycle", tuple(key for key, count in indegree.items() if count > 0))


def _unique(keys: tuple[str, ...], label: str) -> None:
    if len(keys) != len(set(keys)):
        duplicates = tuple(sorted({key for key in keys if keys.count(key) > 1}))
        _fail("proposal_schema_invalid", f"{label} keys must be unique", duplicates)


def _contains_concrete_identity(value: object, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            (
                isinstance(key, str)
                and key in keys
                and item is not None
            )
            or _contains_concrete_identity(item, keys)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_concrete_identity(item, keys) for item in value)
    return False


def _fail(code: str, detail: str, keys: tuple[str, ...] = ()) -> NoReturn:
    raise ProposalValidationError(code, detail, keys)
