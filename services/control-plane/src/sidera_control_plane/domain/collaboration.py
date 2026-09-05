from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from uuid import UUID

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.modules import (
    COLLABORATION_INPUT_GROUPS_STRATEGY_ID,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)
from sidera_control_plane.domain.result_inputs import InputGroupInvalid, InputGroupV1

_MAX_TEXT_LENGTH = 16_384
_MAX_MAP_ENTRIES = 128
_MAX_MAP_DEPTH = 8
_MAX_MAP_BYTES = 128 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class CollaborationMode(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class CollaborationGateMode(StrEnum):
    UNMANAGED = "unmanaged"
    RELEASE_REQUIRED = "release_required"


class CollaborationStrategy(StrEnum):
    ALL_REQUIRED = "all_required"


class CollaborationRunStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class CollaborationEvaluationStatus(StrEnum):
    PENDING = "pending"
    EVALUATING = "evaluating"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class RuntimeIntentApplicationStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"
    STALE = "stale"
    DUPLICATE = "duplicate"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class CollaborationPolicyEntry:
    enabled: bool
    strategy_id: CollaborationStrategy

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValidationError("collaboration policy enabled must be a boolean")
        object.__setattr__(
            self,
            "strategy_id",
            _enum(self.strategy_id, CollaborationStrategy, "collaboration strategy"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"enabled": self.enabled, "strategy_id": self.strategy_id.value}


@dataclass(frozen=True, slots=True)
class CollaborationPolicyOverride:
    subtask_key: str
    enabled: bool
    strategy_id: CollaborationStrategy

    def __post_init__(self) -> None:
        object.__setattr__(self, "subtask_key", _key(self.subtask_key, "subtask_key"))
        if not isinstance(self.enabled, bool):
            raise ValidationError("collaboration override enabled must be a boolean")
        object.__setattr__(
            self,
            "strategy_id",
            _enum(self.strategy_id, CollaborationStrategy, "collaboration strategy"),
        )

    def as_policy(self) -> CollaborationPolicyEntry:
        return CollaborationPolicyEntry(
            enabled=self.enabled,
            strategy_id=self.strategy_id,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "subtask_key": self.subtask_key,
            "enabled": self.enabled,
            "strategy_id": self.strategy_id.value,
        }


@dataclass(frozen=True, slots=True)
class CollaborationPolicyRequestV1:
    mode: CollaborationMode
    default_policy: CollaborationPolicyEntry
    subtask_overrides: tuple[CollaborationPolicyOverride, ...]

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> CollaborationPolicyRequestV1:
        mapping = _strict_mapping(
            value,
            "collaboration policy request",
            {"mode", "default_policy", "subtask_overrides"},
        )
        mode = _enum(
            mapping.get("mode"), CollaborationMode, "collaboration mode"
        )
        default_policy = _policy_entry_from_mapping(mapping.get("default_policy"))
        raw_overrides = mapping.get("subtask_overrides")
        if not isinstance(raw_overrides, (list, tuple)):
            raise ValidationError("subtask_overrides must be an array")
        overrides = tuple(_override_from_mapping(item) for item in raw_overrides)
        keys = [item.subtask_key for item in overrides]
        if len(keys) != len(set(keys)):
            raise ValidationError("duplicate collaboration override")
        if mode is CollaborationMode.DISABLED and overrides:
            raise ValidationError(
                "disabled collaboration policy cannot contain overrides"
            )
        return cls(
            mode=mode,
            default_policy=default_policy,
            subtask_overrides=overrides,
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mode",
            _enum(self.mode, CollaborationMode, "collaboration mode"),
        )
        if not isinstance(self.default_policy, CollaborationPolicyEntry):
            raise ValidationError("default_policy must be a collaboration policy")
        overrides = tuple(self.subtask_overrides)
        if any(not isinstance(item, CollaborationPolicyOverride) for item in overrides):
            raise ValidationError("subtask_overrides contains an invalid override")
        keys = [item.subtask_key for item in overrides]
        if len(keys) != len(set(keys)):
            raise ValidationError("duplicate collaboration override")
        if self.mode is CollaborationMode.DISABLED and overrides:
            raise ValidationError(
                "disabled collaboration policy cannot contain overrides"
            )
        object.__setattr__(self, "subtask_overrides", overrides)

    def validate_for_subtasks(self, subtask_keys: frozenset[str]) -> None:
        if not isinstance(subtask_keys, (frozenset, set)):
            raise ValidationError("subtask_keys must be a set")
        unknown = sorted(
            item.subtask_key
            for item in self.subtask_overrides
            if item.subtask_key not in subtask_keys
        )
        if unknown:
            raise ValidationError(
                f"unknown collaboration override subtask: {', '.join(unknown)}"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "default_policy": {
                "strategy_id": self.default_policy.strategy_id.value
            },
            "subtask_overrides": [
                item.to_mapping() for item in self.subtask_overrides
            ],
        }


@dataclass(frozen=True, slots=True)
class CollaborationPolicyRequestV2:
    schema_version: int
    mode: CollaborationMode
    default_policy: CollaborationPolicyEntry
    subtask_overrides: tuple[CollaborationPolicyOverride, ...]
    input_groups: tuple[InputGroupV1, ...]

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> CollaborationPolicyRequestV2:
        mapping = _strict_mapping(
            value,
            "collaboration policy request",
            {
                "schema_version",
                "mode",
                "default_policy",
                "subtask_overrides",
                "input_groups",
            },
        )
        raw_overrides = mapping.get("subtask_overrides")
        if not isinstance(raw_overrides, (list, tuple)):
            raise ValidationError("subtask_overrides must be an array")
        return cls(
            schema_version=_positive_int(mapping.get("schema_version"), "schema_version"),
            mode=_enum(mapping.get("mode"), CollaborationMode, "collaboration mode"),
            default_policy=_policy_entry_from_mapping(mapping.get("default_policy")),
            subtask_overrides=tuple(
                _override_from_mapping(item) for item in raw_overrides
            ),
            input_groups=_input_groups_from_mapping(mapping.get("input_groups")),
        )

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValidationError("unsupported collaboration policy request schema version")
        object.__setattr__(
            self,
            "mode",
            _enum(self.mode, CollaborationMode, "collaboration mode"),
        )
        if not isinstance(self.default_policy, CollaborationPolicyEntry):
            raise ValidationError("default_policy must be a collaboration policy")
        overrides = tuple(self.subtask_overrides)
        _validate_policy_overrides(overrides)
        groups = _normalize_input_groups(self.input_groups)
        if self.mode is CollaborationMode.DISABLED and (overrides or groups):
            raise ValidationError(
                "disabled collaboration policy cannot contain overrides or input groups"
            )
        object.__setattr__(self, "subtask_overrides", overrides)
        object.__setattr__(self, "input_groups", groups)

    def validate_for_subtasks(self, subtask_keys: frozenset[str]) -> None:
        if not isinstance(subtask_keys, (frozenset, set)):
            raise ValidationError("subtask_keys must be a set")
        unknown = sorted(
            item.subtask_key
            for item in self.subtask_overrides
            if item.subtask_key not in subtask_keys
        )
        if unknown:
            raise ValidationError(
                f"unknown collaboration override subtask: {', '.join(unknown)}"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "default_policy": {
                "strategy_id": self.default_policy.strategy_id.value
            },
            "subtask_overrides": [
                item.to_mapping() for item in self.subtask_overrides
            ],
            "input_groups": [item.to_mapping() for item in self.input_groups],
        }


@dataclass(frozen=True, slots=True)
class CollaborationRuntimeBindingSnapshot:
    runtime_definition_id: UUID
    runtime_instance_id: UUID
    runtime_interface_version: str
    runtime_config_version: int
    runtime_capability_snapshot: CollaborationRuntimeCapabilityProfile

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> CollaborationRuntimeBindingSnapshot:
        mapping = _strict_mapping(
            value,
            "collaboration runtime binding",
            {
                "runtime_definition_id",
                "runtime_instance_id",
                "runtime_interface_version",
                "runtime_config_version",
                "runtime_capability_snapshot",
            },
        )
        capability = _runtime_capability_from_mapping(
            mapping.get("runtime_capability_snapshot")
        )
        return cls(
            runtime_definition_id=_uuid_from_mapping(
                mapping, "runtime_definition_id"
            ),
            runtime_instance_id=_uuid_from_mapping(mapping, "runtime_instance_id"),
            runtime_interface_version=_required_text(
                mapping.get("runtime_interface_version"),
                "runtime_interface_version",
            ),
            runtime_config_version=_positive_int(
                mapping.get("runtime_config_version"), "runtime_config_version"
            ),
            runtime_capability_snapshot=capability,
        )

    def __post_init__(self) -> None:
        _uuid(self.runtime_definition_id, "runtime_definition_id")
        _uuid(self.runtime_instance_id, "runtime_instance_id")
        object.__setattr__(
            self,
            "runtime_interface_version",
            _required_text(
                self.runtime_interface_version, "runtime_interface_version"
            ),
        )
        _positive_int(self.runtime_config_version, "runtime_config_version")
        if not isinstance(
            self.runtime_capability_snapshot, CollaborationRuntimeCapabilityProfile
        ):
            raise ValidationError(
                "runtime_capability_snapshot must be a collaboration runtime profile"
            )
        if (
            self.runtime_interface_version
            != self.runtime_capability_snapshot.interface_version
        ):
            raise ValidationError(
                "runtime interface version must match capability snapshot"
            )

    @property
    def capability_snapshot(self) -> CollaborationRuntimeCapabilityProfile:
        return self.runtime_capability_snapshot

    def to_mapping(self) -> dict[str, object]:
        return {
            "runtime_definition_id": str(self.runtime_definition_id),
            "runtime_instance_id": str(self.runtime_instance_id),
            "runtime_interface_version": self.runtime_interface_version,
            "runtime_config_version": self.runtime_config_version,
            "runtime_capability_snapshot": {
                "interface_version": self.runtime_capability_snapshot.interface_version,
                "projection_schema_versions": list(
                    self.runtime_capability_snapshot.projection_schema_versions
                ),
                "decision_schema_versions": list(
                    self.runtime_capability_snapshot.decision_schema_versions
                ),
                "strategy_ids": list(
                    self.runtime_capability_snapshot.strategy_ids
                ),
                "idempotency": self.runtime_capability_snapshot.idempotency.value,
                "max_projection_bytes": self.runtime_capability_snapshot.max_projection_bytes,
                "max_decision_bytes": self.runtime_capability_snapshot.max_decision_bytes,
                "max_intents_per_decision": self.runtime_capability_snapshot.max_intents_per_decision,
                "max_strategy_state_bytes": self.runtime_capability_snapshot.max_strategy_state_bytes,
                "stability": self.runtime_capability_snapshot.stability.value,
            },
        }


@dataclass(frozen=True, slots=True)
class CollaborationPolicySnapshotV1:
    schema_version: int
    runtime_definition_id: UUID
    runtime_instance_id: UUID
    runtime_interface_version: str
    runtime_config_version: int
    runtime_capability_snapshot: CollaborationRuntimeCapabilityProfile
    default_policy: CollaborationPolicyEntry
    subtask_overrides: tuple[CollaborationPolicyOverride, ...]

    @staticmethod
    def binding_from_mapping(
        value: Mapping[str, object]
    ) -> CollaborationRuntimeBindingSnapshot:
        return CollaborationRuntimeBindingSnapshot.from_mapping(value)

    @classmethod
    def from_request(
        cls,
        request: CollaborationPolicyRequestV1,
        binding: CollaborationRuntimeBindingSnapshot,
    ) -> CollaborationPolicySnapshotV1:
        if request.mode is not CollaborationMode.ENABLED:
            raise ValidationError("only enabled policies can create a snapshot")
        return cls(
            schema_version=1,
            runtime_definition_id=binding.runtime_definition_id,
            runtime_instance_id=binding.runtime_instance_id,
            runtime_interface_version=binding.runtime_interface_version,
            runtime_config_version=binding.runtime_config_version,
            runtime_capability_snapshot=binding.runtime_capability_snapshot,
            default_policy=request.default_policy,
            subtask_overrides=request.subtask_overrides,
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> CollaborationPolicySnapshotV1:
        mapping = _strict_mapping(
            value,
            "collaboration policy snapshot",
            {
                "schema_version",
                "runtime_definition_id",
                "runtime_instance_id",
                "runtime_interface_version",
                "runtime_config_version",
                "runtime_capability_snapshot",
                "default_policy",
                "subtask_overrides",
            },
        )
        binding = CollaborationRuntimeBindingSnapshot.from_mapping(
            {
                name: mapping[name]
                for name in (
                    "runtime_definition_id",
                    "runtime_instance_id",
                    "runtime_interface_version",
                    "runtime_config_version",
                    "runtime_capability_snapshot",
                )
            }
        )
        raw_overrides = mapping.get("subtask_overrides")
        if not isinstance(raw_overrides, (list, tuple)):
            raise ValidationError("subtask_overrides must be an array")
        return cls(
            schema_version=_positive_int(mapping.get("schema_version"), "schema_version"),
            runtime_definition_id=binding.runtime_definition_id,
            runtime_instance_id=binding.runtime_instance_id,
            runtime_interface_version=binding.runtime_interface_version,
            runtime_config_version=binding.runtime_config_version,
            runtime_capability_snapshot=binding.runtime_capability_snapshot,
            default_policy=_policy_entry_from_mapping(
                mapping.get("default_policy"), include_enabled=True
            ),
            subtask_overrides=tuple(_override_from_mapping(item) for item in raw_overrides),
        )

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported collaboration policy schema version")
        binding = CollaborationRuntimeBindingSnapshot(
            runtime_definition_id=self.runtime_definition_id,
            runtime_instance_id=self.runtime_instance_id,
            runtime_interface_version=self.runtime_interface_version,
            runtime_config_version=self.runtime_config_version,
            runtime_capability_snapshot=self.runtime_capability_snapshot,
        )
        object.__setattr__(self, "runtime_definition_id", binding.runtime_definition_id)
        object.__setattr__(self, "runtime_instance_id", binding.runtime_instance_id)
        object.__setattr__(
            self, "runtime_interface_version", binding.runtime_interface_version
        )
        object.__setattr__(
            self,
            "runtime_capability_snapshot",
            binding.runtime_capability_snapshot,
        )
        if not isinstance(self.default_policy, CollaborationPolicyEntry):
            raise ValidationError("default_policy must be a collaboration policy")
        overrides = tuple(self.subtask_overrides)
        if any(not isinstance(item, CollaborationPolicyOverride) for item in overrides):
            raise ValidationError("subtask_overrides contains an invalid override")
        keys = [item.subtask_key for item in overrides]
        if len(keys) != len(set(keys)):
            raise ValidationError("duplicate collaboration override")
        object.__setattr__(self, "subtask_overrides", overrides)

    @property
    def binding(self) -> CollaborationRuntimeBindingSnapshot:
        return CollaborationRuntimeBindingSnapshot(
            runtime_definition_id=self.runtime_definition_id,
            runtime_instance_id=self.runtime_instance_id,
            runtime_interface_version=self.runtime_interface_version,
            runtime_config_version=self.runtime_config_version,
            runtime_capability_snapshot=self.runtime_capability_snapshot,
        )

    def effective_policy(self, subtask_key: str) -> CollaborationPolicyEntry:
        key = _key(subtask_key, "subtask_key")
        for override in self.subtask_overrides:
            if override.subtask_key == key:
                return override.as_policy()
        return self.default_policy

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            **self.binding.to_mapping(),
            "default_policy": self.default_policy.to_mapping(),
            "subtask_overrides": [
                item.to_mapping() for item in self.subtask_overrides
            ],
        }


@dataclass(frozen=True, slots=True)
class CollaborationPolicySnapshotV2:
    schema_version: int
    runtime_definition_id: UUID
    runtime_instance_id: UUID
    runtime_interface_version: str
    runtime_config_version: int
    runtime_capability_snapshot: CollaborationRuntimeCapabilityProfile
    default_policy: CollaborationPolicyEntry
    subtask_overrides: tuple[CollaborationPolicyOverride, ...]
    input_groups: tuple[InputGroupV1, ...]

    @staticmethod
    def binding_from_mapping(
        value: Mapping[str, object]
    ) -> CollaborationRuntimeBindingSnapshot:
        return CollaborationRuntimeBindingSnapshot.from_mapping(value)

    @classmethod
    def from_request(
        cls,
        request: CollaborationPolicyRequestV2,
        binding: CollaborationRuntimeBindingSnapshot,
    ) -> CollaborationPolicySnapshotV2:
        if not isinstance(request, CollaborationPolicyRequestV2):
            raise ValidationError("request must be a collaboration policy request v2")
        if request.mode is not CollaborationMode.ENABLED:
            raise ValidationError("only enabled policies can create a snapshot")
        if not binding.runtime_capability_snapshot.supports_input_groups_v1():
            raise ValidationError(
                f"runtime does not support {COLLABORATION_INPUT_GROUPS_STRATEGY_ID}"
            )
        return cls(
            schema_version=2,
            runtime_definition_id=binding.runtime_definition_id,
            runtime_instance_id=binding.runtime_instance_id,
            runtime_interface_version=binding.runtime_interface_version,
            runtime_config_version=binding.runtime_config_version,
            runtime_capability_snapshot=binding.runtime_capability_snapshot,
            default_policy=request.default_policy,
            subtask_overrides=request.subtask_overrides,
            input_groups=request.input_groups,
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> CollaborationPolicySnapshotV2:
        mapping = _strict_mapping(
            value,
            "collaboration policy snapshot",
            {
                "schema_version",
                "runtime_definition_id",
                "runtime_instance_id",
                "runtime_interface_version",
                "runtime_config_version",
                "runtime_capability_snapshot",
                "default_policy",
                "subtask_overrides",
                "input_groups",
            },
        )
        schema_version = _positive_int(
            mapping.get("schema_version"), "schema_version"
        )
        if schema_version != 2:
            raise ValidationError("unsupported collaboration policy schema version")
        binding = CollaborationRuntimeBindingSnapshot.from_mapping(
            {
                name: mapping[name]
                for name in (
                    "runtime_definition_id",
                    "runtime_instance_id",
                    "runtime_interface_version",
                    "runtime_config_version",
                    "runtime_capability_snapshot",
                )
            }
        )
        raw_overrides = mapping.get("subtask_overrides")
        if not isinstance(raw_overrides, (list, tuple)):
            raise ValidationError("subtask_overrides must be an array")
        return cls(
            schema_version=schema_version,
            runtime_definition_id=binding.runtime_definition_id,
            runtime_instance_id=binding.runtime_instance_id,
            runtime_interface_version=binding.runtime_interface_version,
            runtime_config_version=binding.runtime_config_version,
            runtime_capability_snapshot=binding.runtime_capability_snapshot,
            default_policy=_policy_entry_from_mapping(
                mapping.get("default_policy"), include_enabled=True
            ),
            subtask_overrides=tuple(
                _override_from_mapping(item) for item in raw_overrides
            ),
            input_groups=_input_groups_from_mapping(mapping.get("input_groups")),
        )

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValidationError("unsupported collaboration policy schema version")
        binding = CollaborationRuntimeBindingSnapshot(
            runtime_definition_id=self.runtime_definition_id,
            runtime_instance_id=self.runtime_instance_id,
            runtime_interface_version=self.runtime_interface_version,
            runtime_config_version=self.runtime_config_version,
            runtime_capability_snapshot=self.runtime_capability_snapshot,
        )
        if not binding.runtime_capability_snapshot.supports_input_groups_v1():
            raise ValidationError(
                f"runtime does not support {COLLABORATION_INPUT_GROUPS_STRATEGY_ID}"
            )
        object.__setattr__(self, "runtime_definition_id", binding.runtime_definition_id)
        object.__setattr__(self, "runtime_instance_id", binding.runtime_instance_id)
        object.__setattr__(
            self, "runtime_interface_version", binding.runtime_interface_version
        )
        object.__setattr__(
            self,
            "runtime_capability_snapshot",
            binding.runtime_capability_snapshot,
        )
        if not isinstance(self.default_policy, CollaborationPolicyEntry):
            raise ValidationError("default_policy must be a collaboration policy")
        overrides = tuple(self.subtask_overrides)
        _validate_policy_overrides(overrides)
        object.__setattr__(self, "subtask_overrides", overrides)
        object.__setattr__(self, "input_groups", _normalize_input_groups(self.input_groups))

    @property
    def binding(self) -> CollaborationRuntimeBindingSnapshot:
        return CollaborationRuntimeBindingSnapshot(
            runtime_definition_id=self.runtime_definition_id,
            runtime_instance_id=self.runtime_instance_id,
            runtime_interface_version=self.runtime_interface_version,
            runtime_config_version=self.runtime_config_version,
            runtime_capability_snapshot=self.runtime_capability_snapshot,
        )

    def effective_policy(self, subtask_key: str) -> CollaborationPolicyEntry:
        key = _key(subtask_key, "subtask_key")
        for override in self.subtask_overrides:
            if override.subtask_key == key:
                return override.as_policy()
        return self.default_policy

    def effective_strategy_id(
        self, *, worker_item_key: str, subtask_key: str
    ) -> str | None:
        worker_key = _key(worker_item_key, "worker_item_key")
        policy = self.effective_policy(subtask_key)
        is_input_consumer = any(
            group.consumer_worker_item_key == worker_key
            for group in self.input_groups
        )
        if is_input_consumer:
            if not policy.enabled:
                raise ValidationError(
                    "input group consumer requires an enabled collaboration subtask"
                )
            return COLLABORATION_INPUT_GROUPS_STRATEGY_ID
        if not policy.enabled:
            return None
        return policy.strategy_id.value

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            **self.binding.to_mapping(),
            "default_policy": self.default_policy.to_mapping(),
            "subtask_overrides": [
                item.to_mapping() for item in self.subtask_overrides
            ],
            "input_groups": [item.to_mapping() for item in self.input_groups],
        }


def resolve_collaboration_policy(
    request: CollaborationPolicyRequestV1,
    binding: CollaborationRuntimeBindingSnapshot | None,
    *,
    subtask_keys: frozenset[str],
) -> CollaborationPolicySnapshotV1 | None:
    if not isinstance(request, CollaborationPolicyRequestV1):
        raise ValidationError("request must be a collaboration policy request")
    if request.mode is CollaborationMode.DISABLED:
        return None
    if binding is None:
        raise ValidationError("enabled collaboration policy requires runtime binding")
    request.validate_for_subtasks(subtask_keys)
    supported = set(binding.runtime_capability_snapshot.strategy_ids)
    strategies = {request.default_policy.strategy_id.value}
    strategies.update(item.strategy_id.value for item in request.subtask_overrides)
    unsupported = sorted(strategies - supported)
    if unsupported:
        raise ValidationError(
            f"runtime does not support collaboration strategy: {', '.join(unsupported)}"
        )
    return CollaborationPolicySnapshotV1.from_request(request, binding)


def resolve_collaboration_policy_v2(
    request: CollaborationPolicyRequestV2,
    binding: CollaborationRuntimeBindingSnapshot | None,
    *,
    subtask_keys: frozenset[str],
) -> CollaborationPolicySnapshotV2 | None:
    if not isinstance(request, CollaborationPolicyRequestV2):
        raise ValidationError("request must be a collaboration policy request v2")
    if request.mode is CollaborationMode.DISABLED:
        return None
    if binding is None:
        raise ValidationError("enabled collaboration policy requires runtime binding")
    request.validate_for_subtasks(subtask_keys)
    supported = set(binding.runtime_capability_snapshot.strategy_ids)
    strategies = {request.default_policy.strategy_id.value}
    strategies.update(item.strategy_id.value for item in request.subtask_overrides)
    unsupported = sorted(strategies - supported)
    if unsupported:
        raise ValidationError(
            f"runtime does not support collaboration strategy: {', '.join(unsupported)}"
        )
    return CollaborationPolicySnapshotV2.from_request(request, binding)


@dataclass(slots=True)
class CollaborationRun:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    runtime_definition_id: UUID
    runtime_instance_id: UUID
    runtime_interface_version: str
    runtime_config_version: int
    runtime_capability_snapshot: CollaborationRuntimeCapabilityProfile
    policy_snapshot: Mapping[str, object]
    status: CollaborationRunStatus
    last_projection_snapshot_id: UUID | None
    last_decision_id: UUID | None
    last_evaluated_change_cursor: int
    next_evaluation_at: datetime | None
    consecutive_failure_count: int
    blocked_reason: str | None
    resource_version: int
    created_at: datetime
    activated_at: datetime | None
    completed_at: datetime | None
    superseded_at: datetime | None
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        run_id: UUID,
        workspace_id: UUID,
        root_task_id: UUID,
        plan_snapshot_id: UUID,
        plan_generation: int,
        plan_content_hash: str,
        runtime_definition_id: UUID,
        runtime_instance_id: UUID,
        runtime_interface_version: str,
        runtime_config_version: int,
        runtime_capability_snapshot: CollaborationRuntimeCapabilityProfile,
        policy_snapshot: Mapping[str, object],
        now: datetime,
    ) -> CollaborationRun:
        return cls(
            id=run_id,
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            plan_snapshot_id=plan_snapshot_id,
            plan_generation=plan_generation,
            plan_content_hash=plan_content_hash,
            runtime_definition_id=runtime_definition_id,
            runtime_instance_id=runtime_instance_id,
            runtime_interface_version=runtime_interface_version,
            runtime_config_version=runtime_config_version,
            runtime_capability_snapshot=runtime_capability_snapshot,
            policy_snapshot=policy_snapshot,
            status=CollaborationRunStatus.PENDING,
            last_projection_snapshot_id=None,
            last_decision_id=None,
            last_evaluated_change_cursor=0,
            next_evaluation_at=now,
            consecutive_failure_count=0,
            blocked_reason=None,
            resource_version=1,
            created_at=now,
            activated_at=None,
            completed_at=None,
            superseded_at=None,
            updated_at=now,
        )

    def __post_init__(self) -> None:
        for name in (
            "id",
            "workspace_id",
            "root_task_id",
            "plan_snapshot_id",
            "runtime_definition_id",
            "runtime_instance_id",
        ):
            _uuid(getattr(self, name), name)
        _positive_int(self.plan_generation, "plan_generation")
        _hash(self.plan_content_hash, "plan_content_hash")
        object.__setattr__(
            self,
            "runtime_interface_version",
            _required_text(
                self.runtime_interface_version, "runtime_interface_version"
            ),
        )
        _positive_int(self.runtime_config_version, "runtime_config_version")
        if not isinstance(
            self.runtime_capability_snapshot, CollaborationRuntimeCapabilityProfile
        ):
            raise ValidationError(
                "runtime_capability_snapshot must be a collaboration runtime profile"
            )
        object.__setattr__(
            self,
            "policy_snapshot",
            _freeze_mapping(self.policy_snapshot, "policy_snapshot"),
        )
        object.__setattr__(
            self, "status", _enum(self.status, CollaborationRunStatus, "run status")
        )
        _positive_int(
            self.resource_version, "collaboration run resource_version"
        )
        _non_negative_int(
            self.last_evaluated_change_cursor, "last_evaluated_change_cursor"
        )
        _non_negative_int(
            self.consecutive_failure_count, "consecutive_failure_count"
        )
        for name in ("created_at", "updated_at"):
            _aware(getattr(self, name), name)
        for name in ("next_evaluation_at", "activated_at", "completed_at", "superseded_at"):
            _optional_aware(getattr(self, name), name)
        object.__setattr__(
            self, "blocked_reason", _optional_text(self.blocked_reason, "blocked_reason")
        )

    def activate(self, *, now: datetime) -> None:
        if self.status is not CollaborationRunStatus.PENDING:
            raise InvalidTransition("only a pending collaboration run can activate")
        _aware(now, "collaboration run activation time")
        self.status = CollaborationRunStatus.ACTIVE
        self.activated_at = now
        self.next_evaluation_at = now
        self._bump(now)

    def block(self, *, reason: str, now: datetime) -> None:
        if self.status not in {
            CollaborationRunStatus.PENDING,
            CollaborationRunStatus.ACTIVE,
        }:
            raise InvalidTransition("collaboration run cannot block from its current status")
        _aware(now, "collaboration run blocked time")
        self.status = CollaborationRunStatus.BLOCKED
        self.blocked_reason = _required_text(reason, "blocked_reason")
        self.next_evaluation_at = None
        self.consecutive_failure_count += 1
        self._bump(now)

    def recover_same_binding(self, *, now: datetime) -> None:
        if self.status is not CollaborationRunStatus.BLOCKED:
            raise InvalidTransition("only a blocked collaboration run can recover")
        _aware(now, "collaboration run recovery time")
        self.status = CollaborationRunStatus.ACTIVE
        self.blocked_reason = None
        self.next_evaluation_at = now
        self._bump(now)

    def complete(self, *, now: datetime) -> None:
        if self.status is not CollaborationRunStatus.ACTIVE:
            raise InvalidTransition("only an active collaboration run can complete")
        _aware(now, "collaboration run completion time")
        self.status = CollaborationRunStatus.COMPLETED
        self.completed_at = now
        self.next_evaluation_at = None
        self._bump(now)

    def supersede(self, *, now: datetime) -> None:
        if self.status in {
            CollaborationRunStatus.COMPLETED,
            CollaborationRunStatus.SUPERSEDED,
        }:
            raise InvalidTransition("collaboration run is already terminal")
        _aware(now, "collaboration run superseded time")
        self.status = CollaborationRunStatus.SUPERSEDED
        self.superseded_at = now
        self.next_evaluation_at = None
        self._bump(now)

    def record_projection(
        self,
        *,
        projection_snapshot_id: UUID,
        fact_cursor: int,
        now: datetime,
    ) -> None:
        if self.status is not CollaborationRunStatus.ACTIVE:
            raise InvalidTransition(
                "only an active collaboration run can prepare an evaluation"
            )
        _uuid(projection_snapshot_id, "projection_snapshot_id")
        _non_negative_int(fact_cursor, "fact_cursor")
        if fact_cursor < self.last_evaluated_change_cursor:
            raise ValidationError("projection cursor cannot move backwards")
        _aware(now, "collaboration projection preparation time")
        self.last_projection_snapshot_id = projection_snapshot_id
        self.last_evaluated_change_cursor = fact_cursor
        self.next_evaluation_at = None
        self._bump(now)

    def record_decision(
        self,
        *,
        decision_id: UUID,
        fact_cursor: int,
        now: datetime,
    ) -> None:
        """Record the last applied Decision and consume its wakeups."""
        if self.status is CollaborationRunStatus.PENDING:
            raise InvalidTransition(
                "a pending collaboration run cannot record a Decision"
            )
        _uuid(decision_id, "decision_id")
        _non_negative_int(fact_cursor, "fact_cursor")
        if fact_cursor < self.last_evaluated_change_cursor:
            raise ValidationError("decision cursor cannot move backwards")
        _aware(now, "collaboration decision time")
        self.last_decision_id = decision_id
        self.last_evaluated_change_cursor = fact_cursor
        if self.status is CollaborationRunStatus.ACTIVE:
            self.next_evaluation_at = None
        self._bump(now)

    def _bump(self, now: datetime) -> None:
        self.resource_version += 1
        self.updated_at = now


@dataclass(slots=True)
class CollaborationEvaluation:
    id: UUID
    workspace_id: UUID
    run_id: UUID
    projection_snapshot_id: UUID
    operation_id: UUID
    status: CollaborationEvaluationStatus
    attempt: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    last_error_code: str | None
    response_hash: str | None
    diagnostic_summary: str | None
    resource_version: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def create(
        cls,
        *,
        evaluation_id: UUID,
        workspace_id: UUID,
        run_id: UUID,
        projection_snapshot_id: UUID,
        operation_id: UUID,
        now: datetime,
    ) -> CollaborationEvaluation:
        return cls(
            id=evaluation_id,
            workspace_id=workspace_id,
            run_id=run_id,
            projection_snapshot_id=projection_snapshot_id,
            operation_id=operation_id,
            status=CollaborationEvaluationStatus.PENDING,
            attempt=1,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=now,
            last_error_code=None,
            response_hash=None,
            diagnostic_summary=None,
            resource_version=1,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )

    def __post_init__(self) -> None:
        for name in (
            "id",
            "workspace_id",
            "run_id",
            "projection_snapshot_id",
            "operation_id",
        ):
            _uuid(getattr(self, name), f"evaluation {name}")
        object.__setattr__(
            self,
            "status",
            _enum(self.status, CollaborationEvaluationStatus, "evaluation status"),
        )
        _positive_int(self.attempt, "evaluation attempt")
        _positive_int(self.resource_version, "evaluation resource_version")
        object.__setattr__(
            self, "lease_owner", _optional_text(self.lease_owner, "lease_owner")
        )
        object.__setattr__(
            self,
            "last_error_code",
            _optional_text(self.last_error_code, "last_error_code"),
        )
        object.__setattr__(
            self,
            "diagnostic_summary",
            _optional_text(self.diagnostic_summary, "diagnostic_summary"),
        )
        _optional_hash(self.response_hash, "response_hash")
        for name in ("created_at", "updated_at"):
            _aware(getattr(self, name), f"evaluation {name}")
        for name in ("lease_expires_at", "next_attempt_at", "completed_at"):
            _optional_aware(getattr(self, name), f"evaluation {name}")

    def claim(
        self,
        *,
        owner_id: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> None:
        if self.status not in {
            CollaborationEvaluationStatus.PENDING,
            CollaborationEvaluationStatus.RETRY_WAIT,
        }:
            raise InvalidTransition("evaluation is not claimable")
        owner = _required_text(owner_id, "lease_owner")
        _aware(now, "evaluation claim time")
        _aware(lease_expires_at, "evaluation lease expiry")
        if lease_expires_at <= now:
            raise ValidationError("evaluation lease must expire in the future")
        self.status = CollaborationEvaluationStatus.EVALUATING
        self.lease_owner = owner
        self.lease_expires_at = lease_expires_at
        self.next_attempt_at = None
        self._bump(now)

    def schedule_retry(
        self,
        *,
        error_code: str,
        diagnostic_summary: str | None,
        next_attempt_at: datetime,
        now: datetime,
        response_hash: str | None = None,
    ) -> None:
        if self.status is not CollaborationEvaluationStatus.EVALUATING:
            raise InvalidTransition("only an evaluating evaluation can retry")
        _aware(now, "evaluation retry time")
        _aware(next_attempt_at, "evaluation next attempt time")
        if next_attempt_at < now:
            raise ValidationError("evaluation next attempt cannot be in the past")
        self.status = CollaborationEvaluationStatus.RETRY_WAIT
        self.attempt += 1
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = next_attempt_at
        self.last_error_code = _required_text(error_code, "last_error_code")
        if response_hash is not None:
            _hash(response_hash, "response_hash")
            self.response_hash = response_hash
        self.diagnostic_summary = _optional_text(
            diagnostic_summary, "diagnostic_summary"
        )
        self._bump(now)

    def complete(self, *, now: datetime, response_hash: str) -> None:
        if self.status is not CollaborationEvaluationStatus.EVALUATING:
            raise InvalidTransition("only an evaluating evaluation can complete")
        _aware(now, "evaluation completion time")
        _hash(response_hash, "response_hash")
        self.status = CollaborationEvaluationStatus.COMPLETED
        self.response_hash = response_hash
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = None
        self.completed_at = now
        self._bump(now)

    def record_response_hash(self, *, now: datetime, response_hash: str) -> None:
        """Persist a successful response without closing the phase-C lease."""
        if self.status is not CollaborationEvaluationStatus.EVALUATING:
            raise InvalidTransition(
                "only an evaluating evaluation can record a response"
            )
        _aware(now, "evaluation response time")
        _hash(response_hash, "response_hash")
        self.response_hash = response_hash
        self._bump(now)

    def block(
        self,
        *,
        error_code: str,
        diagnostic_summary: str | None,
        now: datetime,
        response_hash: str | None = None,
    ) -> None:
        if self.status in {
            CollaborationEvaluationStatus.COMPLETED,
            CollaborationEvaluationStatus.BLOCKED,
        }:
            raise InvalidTransition("evaluation is already terminal")
        _aware(now, "evaluation blocked time")
        self.status = CollaborationEvaluationStatus.BLOCKED
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = None
        self.last_error_code = _required_text(error_code, "last_error_code")
        if response_hash is not None:
            _hash(response_hash, "response_hash")
            self.response_hash = response_hash
        self.diagnostic_summary = _optional_text(
            diagnostic_summary, "diagnostic_summary"
        )
        self._bump(now)

    def _bump(self, now: datetime) -> None:
        self.resource_version += 1
        self.updated_at = now


@dataclass(frozen=True, slots=True)
class RuntimeProjectionSnapshot:
    id: UUID
    workspace_id: UUID
    run_id: UUID
    schema_version: int
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    fact_cursor: int
    payload: Mapping[str, object]
    content_hash: str
    size_bytes: int
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "workspace_id", "run_id", "plan_snapshot_id"):
            _uuid(getattr(self, name), f"projection {name}")
        _positive_int(self.schema_version, "projection schema_version")
        _positive_int(self.plan_generation, "projection plan_generation")
        _hash(self.plan_content_hash, "projection plan_content_hash")
        _non_negative_int(self.fact_cursor, "projection fact_cursor")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))
        _hash(self.content_hash, "projection content_hash")
        _positive_int(self.size_bytes, "projection size_bytes")
        _aware(self.created_at, "projection created_at")


@dataclass(frozen=True, slots=True)
class RuntimeDecision:
    id: UUID
    workspace_id: UUID
    run_id: UUID
    evaluation_id: UUID
    projection_snapshot_id: UUID
    projection_hash: str
    operation_id: UUID
    schema_version: int
    sequence: int
    outcome: str
    reason_code: str
    summary: str
    strategy_state: Mapping[str, object]
    content_hash: str
    runtime_duration_ms: int
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "id",
            "workspace_id",
            "run_id",
            "evaluation_id",
            "projection_snapshot_id",
            "operation_id",
        ):
            _uuid(getattr(self, name), f"decision {name}")
        _hash(self.projection_hash, "decision projection_hash")
        _positive_int(self.schema_version, "decision schema_version")
        _positive_int(self.sequence, "decision sequence")
        if self.outcome not in {"intents", "no_change"}:
            raise ValidationError("unsupported decision outcome")
        object.__setattr__(self, "reason_code", _text(self.reason_code, "reason_code"))
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        object.__setattr__(
            self,
            "strategy_state",
            _freeze_mapping(self.strategy_state, "strategy_state"),
        )
        _hash(self.content_hash, "decision content_hash")
        _non_negative_int(self.runtime_duration_ms, "runtime_duration_ms")
        _aware(self.created_at, "decision created_at")


@dataclass(frozen=True, slots=True)
class RuntimeIntent:
    id: UUID
    workspace_id: UUID
    run_id: UUID
    decision_id: UUID
    ordinal: int
    intent_type: str
    target_execution_request_id: UUID
    target_request_version: int
    reason_code: str
    summary: str
    idempotency_key: str
    payload_json: Mapping[str, object]
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "id",
            "workspace_id",
            "run_id",
            "decision_id",
            "target_execution_request_id",
        ):
            _uuid(getattr(self, name), f"intent {name}")
        _non_negative_int(self.ordinal, "intent ordinal")
        if self.intent_type != "release_execution_request":
            raise ValidationError(f"unsupported intent_type: {self.intent_type!r}")
        _positive_int(self.target_request_version, "target_request_version")
        object.__setattr__(self, "reason_code", _text(self.reason_code, "reason_code"))
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        object.__setattr__(
            self, "idempotency_key", _text(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(
            self, "payload_json", _freeze_mapping(self.payload_json, "payload_json")
        )
        _aware(self.created_at, "intent created_at")


@dataclass(frozen=True, slots=True)
class RuntimeIntentApplication:
    id: UUID
    workspace_id: UUID
    intent_id: UUID
    status: RuntimeIntentApplicationStatus
    expected_run_version: int
    observed_run_version: int
    expected_request_version: int
    observed_request_version: int | None
    release_id: UUID | None
    result_code: str
    result_summary: str
    resource_version: int
    created_at: datetime
    applied_at: datetime | None

    def __post_init__(self) -> None:
        for name in ("id", "workspace_id", "intent_id"):
            _uuid(getattr(self, name), f"intent application {name}")
        object.__setattr__(
            self,
            "status",
            _enum(
                self.status,
                RuntimeIntentApplicationStatus,
                "intent application status",
            ),
        )
        for name in (
            "expected_run_version",
            "observed_run_version",
            "expected_request_version",
            "resource_version",
        ):
            _positive_int(getattr(self, name), f"intent application {name}")
        if self.observed_request_version is not None:
            _positive_int(
                self.observed_request_version,
                "intent application observed_request_version",
            )
        _uuid_optional(self.release_id, "intent application release_id")
        object.__setattr__(self, "result_code", _text(self.result_code, "result_code"))
        object.__setattr__(
            self, "result_summary", _text(self.result_summary, "result_summary")
        )
        _aware(self.created_at, "intent application created_at")
        _optional_aware(self.applied_at, "intent application applied_at")

    def resolve(
        self,
        *,
        status: RuntimeIntentApplicationStatus,
        result_code: str,
        result_summary: str,
        release_id: UUID | None,
        now: datetime,
    ) -> RuntimeIntentApplication:
        if self.status is not RuntimeIntentApplicationStatus.DEFERRED:
            raise InvalidTransition("only a deferred intent application can be resolved")
        status = _enum(status, RuntimeIntentApplicationStatus, "intent application status")
        if status not in (
            RuntimeIntentApplicationStatus.APPLIED,
            RuntimeIntentApplicationStatus.REJECTED,
            RuntimeIntentApplicationStatus.STALE,
        ):
            raise InvalidTransition("deferred intent application must resolve to a terminal status")
        if status is RuntimeIntentApplicationStatus.APPLIED:
            if release_id is None:
                raise ValidationError("applied intent application requires release_id")
        elif release_id is not None:
            raise ValidationError("non-applied intent application cannot have release_id")
        _aware(now, "intent application applied_at")
        return replace(
            self,
            status=status,
            release_id=release_id,
            result_code=_text(result_code, "result_code"),
            result_summary=_text(result_summary, "result_summary"),
            resource_version=self.resource_version + 1,
            applied_at=now,
        )


@dataclass(frozen=True, slots=True)
class RuntimeRelease:
    id: UUID
    workspace_id: UUID
    run_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    decision_id: UUID
    intent_id: UUID
    execution_request_id: UUID
    request_version_at_release: int
    issued_at: datetime
    input_selection_id: UUID | None = None
    input_selection_hash: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "id",
            "workspace_id",
            "run_id",
            "plan_snapshot_id",
            "decision_id",
            "intent_id",
            "execution_request_id",
        ):
            _uuid(getattr(self, name), f"release {name}")
        _positive_int(self.plan_generation, "release plan_generation")
        _hash(self.plan_content_hash, "release plan_content_hash")
        _positive_int(
            self.request_version_at_release, "request_version_at_release"
        )
        _aware(self.issued_at, "release issued_at")
        if (self.input_selection_id is None) != (self.input_selection_hash is None):
            raise ValidationError(
                "release input selection id and hash must be provided together"
            )
        if self.input_selection_id is not None:
            _uuid(self.input_selection_id, "release input selection id")
        if self.input_selection_hash is not None:
            _hash(self.input_selection_hash, "release input selection hash")


def _policy_entry_from_mapping(
    value: object, *, include_enabled: bool = False
) -> CollaborationPolicyEntry:
    if not isinstance(value, Mapping):
        raise ValidationError("default_policy must be a mapping")
    allowed = {"strategy_id"}
    if include_enabled:
        allowed.add("enabled")
    mapping = _strict_mapping(value, "collaboration policy", allowed)
    enabled = mapping.get("enabled", True)
    return CollaborationPolicyEntry(
        enabled=_boolean(enabled, "collaboration policy enabled"),
        strategy_id=_enum(
            mapping.get("strategy_id"),
            CollaborationStrategy,
            "collaboration strategy",
        ),
    )


def _input_groups_from_mapping(value: object) -> tuple[InputGroupV1, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("input_groups must be an array")
    groups: list[InputGroupV1] = []
    for item in value:
        if isinstance(item, InputGroupV1):
            groups.append(item)
            continue
        if not isinstance(item, Mapping):
            raise InputGroupInvalid("input group must be a mapping")
        groups.append(InputGroupV1.from_mapping(item))
    return tuple(groups)


def _normalize_input_groups(
    groups: tuple[InputGroupV1, ...],
) -> tuple[InputGroupV1, ...]:
    normalized = tuple(groups)
    if any(not isinstance(item, InputGroupV1) for item in normalized):
        raise InputGroupInvalid("input groups must contain typed groups")
    keys = [item.group_key for item in normalized]
    if len(keys) != len(set(keys)):
        raise InputGroupInvalid("input group keys must be unique")
    return tuple(sorted(normalized, key=lambda item: item.group_key))


def _validate_policy_overrides(
    overrides: tuple[CollaborationPolicyOverride, ...],
) -> None:
    if any(not isinstance(item, CollaborationPolicyOverride) for item in overrides):
        raise ValidationError("subtask_overrides contains an invalid override")
    keys = [item.subtask_key for item in overrides]
    if len(keys) != len(set(keys)):
        raise ValidationError("duplicate collaboration override")


def _override_from_mapping(value: object) -> CollaborationPolicyOverride:
    if not isinstance(value, Mapping):
        raise ValidationError("collaboration override must be a mapping")
    mapping = _strict_mapping(
        value,
        "collaboration override",
        {"subtask_key", "enabled", "strategy_id"},
    )
    return CollaborationPolicyOverride(
        subtask_key=_required_text(mapping.get("subtask_key"), "subtask_key"),
        enabled=_boolean(
            mapping.get("enabled"), "collaboration override enabled"
        ),
        strategy_id=_enum(
            mapping.get("strategy_id"),
            CollaborationStrategy,
            "collaboration strategy",
        ),
    )


def _runtime_capability_from_mapping(
    value: object,
) -> CollaborationRuntimeCapabilityProfile:
    if isinstance(value, CollaborationRuntimeCapabilityProfile):
        return value
    mapping = _strict_mapping(
        value,
        "runtime_capability_snapshot",
        {
            "interface_version",
            "projection_schema_versions",
            "decision_schema_versions",
            "strategy_ids",
            "idempotency",
            "max_projection_bytes",
            "max_decision_bytes",
            "max_intents_per_decision",
            "max_strategy_state_bytes",
            "stability",
        },
    )
    return CollaborationRuntimeCapabilityProfile(
        interface_version=_required_text(
            mapping.get("interface_version"),
            "runtime capability interface_version",
        ),
        projection_schema_versions=_version_tuple_from_mapping(
            mapping.get("projection_schema_versions"),
            "projection_schema_versions",
        ),
        decision_schema_versions=_version_tuple_from_mapping(
            mapping.get("decision_schema_versions"),
            "decision_schema_versions",
        ),
        strategy_ids=_string_tuple_from_mapping(
            mapping.get("strategy_ids"), "strategy_ids"
        ),
        idempotency=_enum(
            mapping.get("idempotency"),
            IdempotencyCapability,
            "idempotency capability",
        ),
        max_projection_bytes=_positive_int(
            mapping.get("max_projection_bytes"), "max_projection_bytes"
        ),
        max_decision_bytes=_positive_int(
            mapping.get("max_decision_bytes"), "max_decision_bytes"
        ),
        max_intents_per_decision=_positive_int(
            mapping.get("max_intents_per_decision"), "max_intents_per_decision"
        ),
        max_strategy_state_bytes=_positive_int(
            mapping.get("max_strategy_state_bytes"), "max_strategy_state_bytes"
        ),
        stability=_enum(mapping.get("stability"), StabilityLevel, "stability level"),
    )
def _strict_mapping(
    value: object, name: str, allowed: set[str]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValidationError(f"unknown {name} fields: {', '.join(unknown)}")
    return cast(Mapping[str, object], value)


def _version_tuple_from_mapping(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationError(f"{name} must be a non-empty array")
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in value):
        raise ValidationError(f"{name} must contain positive integers")
    return tuple(sorted(set(value)))


def _string_tuple_from_mapping(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationError(f"{name} must be a non-empty array")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"{name} must contain non-empty strings")
        normalized = item.strip()
        if normalized in items:
            raise ValidationError(f"{name} must be unique")
        items.append(normalized)
    return tuple(items)


def _freeze_mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    frozen = _freeze_json(value, name, depth=0)
    if not isinstance(frozen, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    if _json_size(frozen) > _MAX_MAP_BYTES:
        raise ValidationError(f"{name} exceeds the maximum JSON size")
    return cast(Mapping[str, object], frozen)


def _freeze_json(value: object, name: str, *, depth: int) -> object:
    if depth > _MAX_MAP_DEPTH:
        raise ValidationError(f"{name} exceeds maximum JSON depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, str):
        if not value.strip():
            raise ValidationError(f"{name} contains an empty string")
        if len(value) > _MAX_TEXT_LENGTH:
            raise ValidationError(f"{name} contains an oversized string")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_MAP_ENTRIES:
            raise ValidationError(f"{name} contains too many entries")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValidationError(f"{name} keys must be non-empty strings")
            result[key] = _freeze_json(item, f"{name}.{key}", depth=depth + 1)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_MAP_ENTRIES:
            raise ValidationError(f"{name} contains too many items")
        return tuple(
            _freeze_json(item, f"{name}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        )
    raise ValidationError(f"{name} contains a non-JSON value")


def _json_size(value: object) -> int:
    return len(
        json.dumps(
            _json_value(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValidationError(f"{name} must be a UUID")
    return value


def _uuid_optional(value: object, name: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, name)


def _uuid_from_mapping(mapping: Mapping[str, object], name: str) -> UUID:
    value = mapping.get(name)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ValidationError(f"{name} must be a UUID") from exc
    raise ValidationError(f"{name} must be a UUID")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    if len(value.strip()) > _MAX_TEXT_LENGTH:
        raise ValidationError(f"{name} is too long")
    return value.strip()


def _text(value: object, name: str) -> str:
    return _required_text(value, name)


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _key(value: object, name: str) -> str:
    normalized = _required_text(value, name)
    if any(character.isspace() for character in normalized):
        raise ValidationError(f"{name} cannot contain whitespace")
    return normalized


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{name} must be positive")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{name} must be non-negative")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value


def _enum[T: StrEnum](value: object, enum_type: type[T], name: str) -> T:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {name}: {value!r}") from exc


def _hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _optional_hash(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _hash(value, name)


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")
    return value


def _optional_aware(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return _aware(value, name)
