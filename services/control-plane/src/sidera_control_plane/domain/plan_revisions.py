from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from sidera_control_plane.domain.errors import (
    PlanGenerationConflict,
    ValidationError,
    VersionConflict,
)

if TYPE_CHECKING:
    from sidera_control_plane.domain.plans import TaskPlanSnapshot


class IdentityStrategy(StrEnum):
    REUSE = "reuse"
    CREATE = "create"
    DERIVE = "derive"


class ContextDisposition(StrEnum):
    UNCHANGED = "unchanged"
    PATCH_REQUIRED = "patch_required"


class WorkerTransitionAction(StrEnum):
    PRESERVE = "preserve"
    MOVE = "move"
    PATCH = "patch"
    CONTINUE = "continue"
    CREATE = "create"
    RETIRE = "retire"


@dataclass(frozen=True, slots=True)
class RevisionBaseDescriptorV1:
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    root_task_version: int
    captured_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("revision base schema_version must be 1")
        for name in (
            "workspace_id",
            "root_task_id",
            "plan_snapshot_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"revision base {name} must be a UUID")
        _positive_int(self.plan_generation, "revision base plan_generation")
        _positive_int(self.root_task_version, "revision base root_task_version")
        _lower_sha256(self.plan_content_hash, "revision base plan_content_hash")
        _aware(self.captured_at, "revision base captured_at")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "plan_snapshot_id": str(self.plan_snapshot_id),
            "plan_generation": self.plan_generation,
            "plan_content_hash": self.plan_content_hash,
            "root_task_version": self.root_task_version,
            "captured_at": self.captured_at.isoformat(),
        }


@dataclass(slots=True)
class RootTaskPlanHead:
    workspace_id: UUID
    root_task_id: UUID
    active_snapshot_id: UUID
    active_generation: int
    active_snapshot_hash: str
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "root_task_id",
            "active_snapshot_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"plan head {name} must be a UUID")
        _positive_int(self.active_generation, "plan head active_generation")
        _lower_sha256(self.active_snapshot_hash, "plan head active_snapshot_hash")
        _positive_int(self.version, "plan head version")
        _aware(self.updated_at, "plan head updated_at")

    def advance(
        self,
        *,
        expected_snapshot_id: UUID,
        expected_version: int,
        target_snapshot_id: UUID,
        target_generation: int,
        target_snapshot_hash: str,
        now: datetime,
    ) -> None:
        if self.version != expected_version:
            raise VersionConflict(expected_version, self.version)
        if self.active_snapshot_id != expected_snapshot_id:
            raise ValidationError("plan head active snapshot does not match expected base")
        if target_snapshot_id == self.active_snapshot_id:
            raise ValidationError("plan head target snapshot must be new")
        if target_generation != self.active_generation + 1:
            raise PlanGenerationConflict(self.active_generation + 1, target_generation)
        _lower_sha256(target_snapshot_hash, "plan head target_snapshot_hash")
        _aware(now, "plan head updated_at")
        self.active_snapshot_id = target_snapshot_id
        self.active_generation = target_generation
        self.active_snapshot_hash = target_snapshot_hash
        self.version += 1
        self.updated_at = now


@dataclass(frozen=True, slots=True)
class SubtaskIdentityDecision:
    target_subtask_key: str
    strategy: IdentityStrategy
    existing_subtask_task_id: UUID | None
    derived_from_subtask_task_ids: tuple[UUID, ...]
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_subtask_key", _key(self.target_subtask_key))
        object.__setattr__(
            self,
            "strategy",
            _enum_value(self.strategy, IdentityStrategy, "subtask identity strategy"),
        )
        _optional_uuid(self.existing_subtask_task_id, "existing_subtask_task_id")
        sources = _uuid_tuple(
            self.derived_from_subtask_task_ids,
            "derived_from_subtask_task_ids",
        )
        object.__setattr__(self, "derived_from_subtask_task_ids", sources)
        object.__setattr__(self, "reason", _required_text(self.reason, "identity reason"))
        _validate_identity_shape(
            strategy=self.strategy,
            existing=self.existing_subtask_task_id,
            derived=sources,
            label="subtask",
        )


@dataclass(frozen=True, slots=True)
class WorkerItemIdentityDecision:
    target_worker_item_key: str
    strategy: IdentityStrategy
    existing_worker_item_id: UUID | None
    derived_from_worker_item_ids: tuple[UUID, ...]
    target_subtask_key: str
    context_disposition: ContextDisposition
    change_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_worker_item_key", _key(self.target_worker_item_key)
        )
        object.__setattr__(
            self,
            "strategy",
            _enum_value(self.strategy, IdentityStrategy, "worker identity strategy"),
        )
        _optional_uuid(self.existing_worker_item_id, "existing_worker_item_id")
        sources = _uuid_tuple(
            self.derived_from_worker_item_ids,
            "derived_from_worker_item_ids",
        )
        object.__setattr__(self, "derived_from_worker_item_ids", sources)
        object.__setattr__(self, "target_subtask_key", _key(self.target_subtask_key))
        object.__setattr__(
            self,
            "context_disposition",
            _enum_value(
                self.context_disposition,
                ContextDisposition,
                "context disposition",
            ),
        )
        reasons = _text_tuple(self.change_reasons, "change_reasons")
        object.__setattr__(self, "change_reasons", reasons)
        _validate_identity_shape(
            strategy=self.strategy,
            existing=self.existing_worker_item_id,
            derived=sources,
            label="WorkerItem",
        )
        if (
            self.context_disposition is ContextDisposition.PATCH_REQUIRED
            and self.strategy is not IdentityStrategy.REUSE
        ):
            raise ValidationError("only a reused WorkerItem can require a context patch")


ACTIVE_REVISION_STATUSES = frozenset(
    {
        "submitted",
        "planning",
        "waiting_for_user",
        "proposal_ready",
        "failed",
    }
)
TERMINAL_REVISION_STATUSES = frozenset(
    {"materialized", "conflicted", "rejected", "cancelled"}
)


def validate_subtask_identity_decisions(
    decisions: tuple[SubtaskIdentityDecision, ...],
    *,
    retired_subtask_task_ids: tuple[UUID, ...] = (),
    reworked_terminal_subtask_ids: tuple[UUID, ...] = (),
) -> None:
    target_keys: set[str] = set()
    retired = set(_uuid_tuple(retired_subtask_task_ids, "retired_subtask_task_ids"))
    reworked = set(
        _uuid_tuple(reworked_terminal_subtask_ids, "reworked_terminal_subtask_ids")
    )
    for decision in decisions:
        if decision.target_subtask_key in target_keys:
            raise ValidationError("each target subtask key needs one identity decision")
        target_keys.add(decision.target_subtask_key)
        existing = decision.existing_subtask_task_id
        if existing in retired:
            raise ValidationError("retired subtask identity cannot be reused")
        if existing in reworked and decision.strategy is not IdentityStrategy.DERIVE:
            raise ValidationError("reworked terminal subtask must derive a new identity")


def validate_worker_item_identity_decisions(
    decisions: tuple[WorkerItemIdentityDecision, ...],
    *,
    retired_worker_item_ids: tuple[UUID, ...] = (),
) -> None:
    target_keys: set[str] = set()
    reused_ids: set[UUID] = set()
    retired = set(_uuid_tuple(retired_worker_item_ids, "retired_worker_item_ids"))
    for decision in decisions:
        if decision.target_worker_item_key in target_keys:
            raise ValidationError(
                "each target WorkerItem key needs one identity decision"
            )
        target_keys.add(decision.target_worker_item_key)
        existing = decision.existing_worker_item_id
        if existing is None:
            continue
        if existing in retired:
            raise ValidationError("retired WorkerItem identity cannot be reused")
        if existing in reused_ids:
            raise ValidationError("one existing WorkerItem can have one target")
        reused_ids.add(existing)


def plan_snapshot_content_hash(snapshot: TaskPlanSnapshot) -> str:
    encoded = json.dumps(
        snapshot.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_identity_shape(
    *,
    strategy: IdentityStrategy,
    existing: UUID | None,
    derived: tuple[UUID, ...],
    label: str,
) -> None:
    if strategy is IdentityStrategy.REUSE and (existing is None or derived):
        raise ValidationError(f"reused {label} requires one existing identity only")
    if strategy is IdentityStrategy.CREATE and (existing is not None or derived):
        raise ValidationError(f"created {label} cannot reference an identity")
    if strategy is IdentityStrategy.DERIVE and (existing is not None or not derived):
        raise ValidationError(f"derived {label} requires one or more source identities")


def _enum_value(value: object, enum_type: type[StrEnum], label: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {label}: {value!r}") from exc


def _key(value: str) -> str:
    return _required_text(value, "identity target key")


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required")
    return value.strip()


def _text_tuple(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValidationError(f"{label} must be a sequence")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _required_text(value, label)
        if clean not in seen:
            result.append(clean)
            seen.add(clean)
    return tuple(result)


def _uuid_tuple(values: tuple[UUID, ...], label: str) -> tuple[UUID, ...]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{label} must be a sequence of UUIDs")
    result = tuple(values)
    if any(not isinstance(value, UUID) for value in result):
        raise ValidationError(f"{label} must contain UUIDs")
    if len(set(result)) != len(result):
        raise ValidationError(f"{label} must not contain duplicates")
    return result


def _optional_uuid(value: UUID | None, label: str) -> None:
    if value is not None and not isinstance(value, UUID):
        raise ValidationError(f"{label} must be a UUID")


def _positive_int(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{label} must be positive")


def _aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError(f"{label} must be timezone-aware")


def _lower_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError(f"{label} must be a lowercase SHA-256 value")
