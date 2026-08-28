from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError


@dataclass(frozen=True, slots=True)
class ContextPatchV1:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    worker_item_id: UUID
    plan_transition_id: UUID
    worker_transition_id: UUID
    base_plan_snapshot_id: UUID
    base_generation: int
    target_plan_snapshot_id: UUID
    target_generation: int
    base_context_package_id: UUID
    base_package_hash: str
    base_work_context_revision_id: UUID
    base_context_hash: str
    target_context_version: int
    scope: Literal["worker_item"]
    changed_requirements: tuple[str, ...]
    changed_acceptance_refs: tuple[str, ...]
    changed_dependency_refs: tuple[str, ...]
    decision_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    intervention_refs: tuple[str, ...]
    unfinished_work: str
    checkpoint_ref: str
    patch_hash: str
    proposer: str
    expires_at: datetime
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        workspace_id: UUID,
        root_task_id: UUID,
        worker_item_id: UUID,
        plan_transition_id: UUID,
        worker_transition_id: UUID,
        base_plan_snapshot_id: UUID,
        base_generation: int,
        target_plan_snapshot_id: UUID,
        target_generation: int,
        base_context_package_id: UUID,
        base_package_hash: str,
        base_work_context_revision_id: UUID,
        base_context_hash: str,
        target_context_version: int,
        changed_requirements: tuple[str, ...],
        changed_acceptance_refs: tuple[str, ...],
        changed_dependency_refs: tuple[str, ...],
        decision_refs: tuple[str, ...],
        artifact_refs: tuple[str, ...],
        evidence_refs: tuple[str, ...],
        intervention_refs: tuple[str, ...],
        unfinished_work: str,
        checkpoint_ref: str,
        proposer: str,
        expires_at: datetime,
        created_at: datetime,
    ) -> ContextPatchV1:
        values = {
            "id": id,
            "workspace_id": workspace_id,
            "root_task_id": root_task_id,
            "worker_item_id": worker_item_id,
            "plan_transition_id": plan_transition_id,
            "worker_transition_id": worker_transition_id,
            "base_plan_snapshot_id": base_plan_snapshot_id,
            "base_generation": base_generation,
            "target_plan_snapshot_id": target_plan_snapshot_id,
            "target_generation": target_generation,
            "base_context_package_id": base_context_package_id,
            "base_package_hash": base_package_hash,
            "base_work_context_revision_id": base_work_context_revision_id,
            "base_context_hash": base_context_hash,
            "target_context_version": target_context_version,
            "scope": "worker_item",
            "changed_requirements": changed_requirements,
            "changed_acceptance_refs": changed_acceptance_refs,
            "changed_dependency_refs": changed_dependency_refs,
            "decision_refs": decision_refs,
            "artifact_refs": artifact_refs,
            "evidence_refs": evidence_refs,
            "intervention_refs": intervention_refs,
            "unfinished_work": unfinished_work,
            "checkpoint_ref": checkpoint_ref,
            "proposer": proposer,
            "expires_at": expires_at,
            "created_at": created_at,
        }
        patch_hash = _hash_payload(_patch_hash_values_from_mapping(values))
        return cls(**values, patch_hash=patch_hash)

    def __post_init__(self) -> None:
        for name in (
            "id",
            "workspace_id",
            "root_task_id",
            "worker_item_id",
            "plan_transition_id",
            "worker_transition_id",
            "base_plan_snapshot_id",
            "target_plan_snapshot_id",
            "base_context_package_id",
            "base_work_context_revision_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"context patch {name} must be a UUID")
        _positive(self.base_generation, "base_generation")
        _positive(self.target_generation, "target_generation")
        if self.target_generation != self.base_generation + 1:
            raise ValidationError("context patch generations must be consecutive")
        _positive(self.target_context_version, "target_context_version")
        if self.scope != "worker_item":
            raise ValidationError("context patch scope must be worker_item")
        for name in (
            "base_package_hash",
            "base_context_hash",
            "patch_hash",
        ):
            _lower_hash(getattr(self, name), name)
        for name in (
            "changed_requirements",
            "changed_acceptance_refs",
            "changed_dependency_refs",
            "decision_refs",
            "artifact_refs",
            "evidence_refs",
            "intervention_refs",
        ):
            object.__setattr__(self, name, _text_tuple(getattr(self, name), name))
        for name in ("unfinished_work", "checkpoint_ref", "proposer"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        _aware(self.created_at, "context patch created_at")
        _aware(self.expires_at, "context patch expires_at")
        if self.expires_at <= self.created_at:
            raise ValidationError("context patch expires_at must be after created_at")
        if self.patch_hash != self.content_hash:
            raise ValidationError("context patch hash does not match content")

    @property
    def content_hash(self) -> str:
        return _hash_payload(_patch_hash_values(self))

    def to_mapping(self) -> dict[str, object]:
        return {
            **_patch_values(self),
            "patch_hash": self.patch_hash,
        }

    def assert_deliverable(
        self,
        *,
        now: datetime,
        acknowledged_package_id: UUID,
        acknowledged_package_hash: str,
    ) -> None:
        _aware(now, "context patch delivery time")
        if now >= self.expires_at:
            raise ValidationError("context patch is expired")
        if acknowledged_package_id != self.base_context_package_id:
            raise ValidationError("context patch base package does not match")
        if acknowledged_package_hash != self.base_package_hash:
            raise ValidationError("context patch base package hash does not match")


@dataclass(frozen=True, slots=True)
class ContextPatchAcknowledgement:
    patch_id: UUID
    patch_hash: str
    execution_id: UUID
    session_id: UUID
    operation_id: UUID
    accepted_sequence: int
    adapter_ref: str
    acknowledged_at: datetime

    @classmethod
    def create(
        cls,
        *,
        patch: ContextPatchV1,
        execution_id: UUID,
        session_id: UUID,
        operation_id: UUID,
        accepted_sequence: int,
        adapter_ref: str,
        acknowledged_at: datetime,
    ) -> ContextPatchAcknowledgement:
        return cls(
            patch_id=patch.id,
            patch_hash=patch.patch_hash,
            execution_id=execution_id,
            session_id=session_id,
            operation_id=operation_id,
            accepted_sequence=accepted_sequence,
            adapter_ref=adapter_ref,
            acknowledged_at=acknowledged_at,
        )

    def __post_init__(self) -> None:
        for name in ("patch_id", "execution_id", "session_id", "operation_id"):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"patch acknowledgement {name} must be a UUID")
        _lower_hash(self.patch_hash, "patch_hash")
        if self.accepted_sequence < 0:
            raise ValidationError("accepted_sequence cannot be negative")
        _required(self.adapter_ref, "adapter_ref")
        _aware(self.acknowledged_at, "acknowledged_at")

    def assert_matches(
        self,
        patch: ContextPatchV1,
        *,
        execution_id: UUID,
        session_id: UUID,
        patch_hash: str | None = None,
    ) -> None:
        expected_hash = patch.patch_hash if patch_hash is None else patch_hash
        if self.patch_id != patch.id or self.patch_hash != expected_hash:
            raise ValidationError("patch acknowledgement hash does not match")
        if self.execution_id != execution_id or self.session_id != session_id:
            raise ValidationError("patch acknowledgement execution/session does not match")


@dataclass(frozen=True, slots=True)
class ContextPatchMaterializationV1:
    schema_version: Literal[1]
    patch: ContextPatchV1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("context patch materialization schema_version must be 1")

    def to_mapping(self) -> dict[str, object]:
        return {"schema_version": 1, "patch": self.patch.to_mapping()}


@dataclass(frozen=True, slots=True)
class ContextPatchDeliveryResult:
    outcome: Literal["delivered", "blocked", "failed", "unknown"]
    operation_id: UUID
    patch_hash: str
    accepted_sequence: int | None
    error_code: str | None

    def __post_init__(self) -> None:
        if self.outcome not in {"delivered", "blocked", "failed", "unknown"}:
            raise ValidationError(f"unsupported context patch outcome: {self.outcome!r}")
        if not isinstance(self.operation_id, UUID):
            raise ValidationError("context patch operation_id must be a UUID")
        _lower_hash(self.patch_hash, "patch_hash")
        if self.accepted_sequence is not None and self.accepted_sequence < 0:
            raise ValidationError("accepted_sequence cannot be negative")
        if self.outcome == "delivered" and self.accepted_sequence is None:
            raise ValidationError("delivered context patch requires accepted_sequence")
        if self.error_code is not None:
            _required(self.error_code, "error_code")


def _patch_values(value: ContextPatchV1) -> dict[str, object]:
    return {
        "id": str(value.id),
        "workspace_id": str(value.workspace_id),
        "root_task_id": str(value.root_task_id),
        "worker_item_id": str(value.worker_item_id),
        "plan_transition_id": str(value.plan_transition_id),
        "worker_transition_id": str(value.worker_transition_id),
        "base_plan_snapshot_id": str(value.base_plan_snapshot_id),
        "base_generation": value.base_generation,
        "target_plan_snapshot_id": str(value.target_plan_snapshot_id),
        "target_generation": value.target_generation,
        "base_context_package_id": str(value.base_context_package_id),
        "base_package_hash": value.base_package_hash,
        "base_work_context_revision_id": str(value.base_work_context_revision_id),
        "base_context_hash": value.base_context_hash,
        "target_context_version": value.target_context_version,
        "scope": value.scope,
        "changed_requirements": list(value.changed_requirements),
        "changed_acceptance_refs": list(value.changed_acceptance_refs),
        "changed_dependency_refs": list(value.changed_dependency_refs),
        "decision_refs": list(value.decision_refs),
        "artifact_refs": list(value.artifact_refs),
        "evidence_refs": list(value.evidence_refs),
        "intervention_refs": list(value.intervention_refs),
        "unfinished_work": value.unfinished_work,
        "checkpoint_ref": value.checkpoint_ref,
        "proposer": value.proposer,
        "expires_at": value.expires_at.astimezone(UTC).isoformat(),
        "created_at": value.created_at.astimezone(UTC).isoformat(),
    }


def _patch_hash_values(value: ContextPatchV1) -> dict[str, object]:
    """Hash only the immutable patch content, not storage/timing metadata."""
    return _patch_hash_values_from_mapping(
        {field: getattr(value, field) for field in value.__dataclass_fields__}
    )


def _patch_hash_values_from_mapping(value: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_id": str(value["workspace_id"]),
        "root_task_id": str(value["root_task_id"]),
        "worker_item_id": str(value["worker_item_id"]),
        "plan_transition_id": str(value["plan_transition_id"]),
        "worker_transition_id": str(value["worker_transition_id"]),
        "base_plan_snapshot_id": str(value["base_plan_snapshot_id"]),
        "base_generation": value["base_generation"],
        "target_plan_snapshot_id": str(value["target_plan_snapshot_id"]),
        "target_generation": value["target_generation"],
        "base_context_package_id": str(value["base_context_package_id"]),
        "base_package_hash": value["base_package_hash"],
        "base_work_context_revision_id": str(value["base_work_context_revision_id"]),
        "base_context_hash": value["base_context_hash"],
        "target_context_version": value["target_context_version"],
        "scope": value["scope"],
        "change_set": {
            "changed_requirements": _normalized_refs(value["changed_requirements"]),
            "changed_acceptance_refs": _normalized_refs(value["changed_acceptance_refs"]),
            "changed_dependency_refs": _normalized_refs(value["changed_dependency_refs"]),
            "decision_refs": _normalized_refs(value["decision_refs"]),
            "artifact_refs": _normalized_refs(value["artifact_refs"]),
            "evidence_refs": _normalized_refs(value["evidence_refs"]),
            "intervention_refs": _normalized_refs(value["intervention_refs"]),
            "unfinished_work": value["unfinished_work"],
            "checkpoint_ref": value["checkpoint_ref"],
        },
    }


def _normalized_refs(value: object) -> list[str]:
    return sorted(set(cast(Iterable[str], value)))


def _hash_payload(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            _canonical_value(value),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _text_tuple(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValidationError(f"{label} must be a sequence")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _required(value, label)
        if clean not in seen:
            result.append(clean)
            seen.add(clean)
    return tuple(result)


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required")
    return value.strip()


def _positive(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{label} must be positive")


def _aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError(f"{label} must be timezone-aware")


def _lower_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError(f"{label} must be a lowercase SHA-256 value")
