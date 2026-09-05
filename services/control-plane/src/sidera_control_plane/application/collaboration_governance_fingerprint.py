from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sidera_control_plane.domain.collaboration import RuntimeIntent
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeProjectionV1,
    RuntimeProjectionV2,
    canonical_json_bytes,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
)

_VERSION = 1
_SELECTION_VERSION = 2
_VOLATILE_KEYS = frozenset(
    {
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "fact_cursor",
        "projection_id",
        "summary",
        "reason",
        "gate_reason",
        "eligibility_reason",
        "trace",
        "trace_body",
        "conversation",
        "user_message",
    }
)


@dataclass(frozen=True, slots=True)
class ReleaseProposalFingerprint:
    version: int
    canonical_input: Mapping[str, object]
    value: str


def build_release_proposal_fingerprint(
    projection: RuntimeProjectionV1 | RuntimeProjectionV2,
    intent: RuntimeIntent,
    *,
    worker_item_id: UUID,
    target_handoff_fence: Mapping[str, object] | None = None,
    governance_fence: Mapping[str, object] | None = None,
    selection: CollaborationInputSelectionV1 | None = None,
) -> ReleaseProposalFingerprint:
    if (
        projection.workspace_id != intent.workspace_id
        or projection.run_id != intent.run_id
    ):
        raise ValidationError(
            "projection and intent governance identity does not match"
        )
    target_request_id = str(intent.target_execution_request_id)
    target_request = _find_by_id(
        projection.execution_requests, target_request_id, "target execution request"
    )
    if selection is not None:
        return _selection_fingerprint(
            projection,
            intent,
            selection=selection,
            worker_item_id=worker_item_id,
            target_request=target_request,
            target_handoff_fence=target_handoff_fence,
            governance_fence=governance_fence,
        )
    target_worker = next(
        (
            item
            for item in projection.worker_items
            if item.get("id") == str(worker_item_id)
        ),
        {
            "id": str(worker_item_id),
            "key": target_request.get("worker_item_key"),
        },
    )
    dependencies = tuple(
        item
        for item in projection.dependencies
        if item.get("dependency_type") == "execution_request"
        and item.get("downstream_request_id") == target_request_id
    )
    target_dependencies = _stable_sorted(
        _without_volatile(item) for item in dependencies
    )
    hard_dependencies = tuple(
        item for item in dependencies if item.get("enforcement") == "hard"
    )
    upstream_ids = {
        str(item["upstream_request_id"])
        for item in hard_dependencies
        if isinstance(item.get("upstream_request_id"), str)
    }
    upstream_request_rows = tuple(
        item for item in projection.execution_requests if item.get("id") in upstream_ids
    )
    upstream_requests = _stable_sorted(
        _without_volatile(item) for item in upstream_request_rows
    )

    upstream_execution_ids: set[str] = set()
    upstream_lineage_ids: set[str] = set()
    for request in upstream_request_rows:
        lineage_id = request.get("execution_lineage_id")
        if isinstance(lineage_id, str):
            upstream_lineage_ids.add(lineage_id)
        first_execution_id = request.get("first_execution_id")
        if isinstance(first_execution_id, str):
            upstream_execution_ids.add(first_execution_id)
    upstream_execution_rows = tuple(
        item
        for item in projection.executions
        if item.get("lineage_id") in upstream_lineage_ids
        or item.get("id") in upstream_execution_ids
    )
    upstream_executions = _stable_sorted(
        _without_volatile(item) for item in upstream_execution_rows
    )
    upstream_execution_ids.update(
        str(item["id"])
        for item in upstream_execution_rows
        if isinstance(item.get("id"), str)
    )

    artifacts = _stable_sorted(
        _without_volatile(item)
        for item in projection.artifacts
        if item.get("execution_id") in upstream_execution_ids
    )
    evidence = _stable_sorted(
        _without_volatile(item)
        for item in projection.evidence
        if item.get("execution_id") in upstream_execution_ids
    )
    policy = _target_policy(projection.policy, target_request)
    handoff_fence = target_handoff_fence
    if handoff_fence is None:
        candidate = target_request.get("handoff_fence")
        handoff_fence = candidate if isinstance(candidate, Mapping) else {}

    canonical_input: Mapping[str, object] = {
        "schema_version": _VERSION,
        "workspace_id": str(projection.workspace_id),
        "run_id": str(projection.run_id),
        "plan_snapshot_id": str(projection.plan_snapshot_id),
        "plan_generation": projection.plan_generation,
        "plan_content_hash": projection.plan_content_hash,
        "worker_item_id": str(worker_item_id),
        "worker_item": _without_volatile(target_worker),
        "execution_request": _without_volatile(target_request),
        "policy": _without_volatile(policy),
        "dependencies": target_dependencies,
        "upstream_requests": upstream_requests,
        "upstream_executions": upstream_executions,
        "artifacts": artifacts,
        "evidence": evidence,
        "handoff_fence": _without_volatile(handoff_fence),
        "intent": {
            "intent_type": intent.intent_type,
            "target_execution_request_id": target_request_id,
            "target_request_version": intent.target_request_version,
        },
    }
    value = sha256(canonical_json_bytes(canonical_input)).hexdigest()
    return ReleaseProposalFingerprint(
        version=_VERSION,
        canonical_input=canonical_input,
        value=value,
    )


def _selection_fingerprint(
    projection: RuntimeProjectionV1 | RuntimeProjectionV2,
    intent: RuntimeIntent,
    *,
    selection: CollaborationInputSelectionV1,
    worker_item_id: UUID,
    target_request: Mapping[str, object],
    target_handoff_fence: Mapping[str, object] | None,
    governance_fence: Mapping[str, object] | None,
) -> ReleaseProposalFingerprint:
    if (
        selection.workspace_id != projection.workspace_id
        or selection.root_task_id != projection.root_task_id
        or selection.run_id != projection.run_id
        or selection.plan_snapshot_id != projection.plan_snapshot_id
        or selection.plan_generation != projection.plan_generation
        or selection.plan_content_hash != projection.plan_content_hash
        or selection.target_worker_item_id != worker_item_id
        or selection.target_execution_request_id != intent.target_execution_request_id
        or selection.target_request_version != intent.target_request_version
        or selection.decision_id != intent.decision_id
        or selection.intent_id != intent.id
        or selection.selection_hash != selection.compute_selection_hash()
    ):
        raise ValidationError(
            "selection and governance proposal identity does not match"
        )
    handoff_fence = target_handoff_fence
    if handoff_fence is None:
        candidate = target_request.get("handoff_fence")
        handoff_fence = candidate if isinstance(candidate, Mapping) else {}
    target = {
        key: target_request.get(key)
        for key in (
            "id",
            "worker_item_id",
            "worker_item_key",
            "subtask_key",
            "collaboration_gate_mode",
            "expected_artifact_refs",
            "expected_evidence_refs",
        )
        if key in target_request
    }
    canonical_input: Mapping[str, object] = {
        "schema_version": _SELECTION_VERSION,
        "workspace_id": str(selection.workspace_id),
        "root_task_id": str(selection.root_task_id),
        "run_id": str(selection.run_id),
        "plan_snapshot_id": str(selection.plan_snapshot_id),
        "plan_generation": selection.plan_generation,
        "plan_content_hash": selection.plan_content_hash,
        "target": _without_volatile(target),
        "target_request_version": selection.target_request_version,
        "input_selection_id": str(selection.id),
        "input_selection_hash": selection.selection_hash,
        "groups": tuple(item.to_mapping() for item in selection.groups),
        "hard_inputs": tuple(item.to_mapping() for item in selection.hard_inputs),
        "governance_fence": _without_volatile(governance_fence or {}),
        "handoff_fence": _without_volatile(handoff_fence),
        "intent": {
            "intent_type": intent.intent_type,
            "target_execution_request_id": str(intent.target_execution_request_id),
            "target_request_version": intent.target_request_version,
        },
    }
    return ReleaseProposalFingerprint(
        version=_SELECTION_VERSION,
        canonical_input=canonical_input,
        value=sha256(canonical_json_bytes(canonical_input)).hexdigest(),
    )


def _find_by_id(
    values: tuple[Mapping[str, object], ...], identity: str, name: str
) -> Mapping[str, object]:
    for item in values:
        if item.get("id") == identity:
            return item
    raise ValidationError(f"{name} is missing from projection")


def _target_policy(
    policy: Mapping[str, object], target_request: Mapping[str, object]
) -> Mapping[str, object]:
    subtask_key = target_request.get("subtask_key")
    selected: object = policy.get("default_policy", policy)
    overrides = policy.get("subtask_overrides")
    if isinstance(overrides, (list, tuple)) and isinstance(subtask_key, str):
        for override in overrides:
            if (
                isinstance(override, Mapping)
                and override.get("subtask_key") == subtask_key
            ):
                selected = override
                break
    return selected if isinstance(selected, Mapping) else {}


def _without_volatile(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _without_volatile(item)
            for key, item in value.items()
            if str(key).lower() not in _VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return tuple(_without_volatile(item) for item in value)
    return value


def _stable_sorted(values: Iterable[object]) -> tuple[object, ...]:
    return tuple(sorted(values, key=canonical_json_bytes))
