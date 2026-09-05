from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    PLANNER_ADAPTER_MODULE_TYPE,
    PlannerAdapterCapabilityProfile,
    canonical_module_type,
)


class PlanningProfileStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class UnknownOutcomePolicy(StrEnum):
    BLOCK = "block"
    RETRY = "retry"


class InvalidOutputPolicy(StrEnum):
    BLOCK = "block"
    RETRY = "retry"


COLLABORATION_MAX_PROJECTION_BYTES = 1_048_576
COLLABORATION_MAX_DECISION_BYTES = 262_144
COLLABORATION_MAX_INTENTS_PER_DECISION = 1_000
COLLABORATION_MAX_STRATEGY_STATE_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class PlanningRetryPolicy:
    max_retries: int
    backoff_seconds: tuple[int, ...]
    unknown_outcome_policy: UnknownOutcomePolicy
    invalid_output_policy: InvalidOutputPolicy

    def __post_init__(self) -> None:
        _positive_or_zero(self.max_retries, "max_retries")
        backoff = _positive_tuple(
            self.backoff_seconds,
            "backoff_seconds",
            allow_empty=self.max_retries == 0,
        )
        if self.max_retries > 0 and not backoff:
            raise ValidationError("backoff_seconds is required when retries are enabled")
        object.__setattr__(self, "backoff_seconds", backoff)
        object.__setattr__(
            self,
            "unknown_outcome_policy",
            _enum_value(
                self.unknown_outcome_policy,
                UnknownOutcomePolicy,
                "unknown outcome policy",
            ),
        )
        object.__setattr__(
            self,
            "invalid_output_policy",
            _enum_value(
                self.invalid_output_policy,
                InvalidOutputPolicy,
                "invalid output policy",
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanningBudgetPolicy:
    max_context_bytes: int
    max_tokens: int
    max_cost_microunits: int
    max_runtime_seconds: int
    max_revisions: int
    max_nodes: int
    max_concurrency: int

    def __post_init__(self) -> None:
        for name in (
            "max_context_bytes",
            "max_tokens",
            "max_cost_microunits",
            "max_runtime_seconds",
            "max_revisions",
            "max_nodes",
            "max_concurrency",
        ):
            _positive(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class PlanningProfileSnapshot:
    profile_id: UUID
    workspace_id: UUID
    profile_key: str
    profile_version: int
    display_name: str
    planner_module_instance_id: UUID
    planner_module_config_version: int
    planner_interface_version: str
    prompt_policy_version: str
    system_policy: Mapping[str, object]
    proposal_schema_version: int
    allowed_capabilities: tuple[str, ...]
    adapter_preferences: tuple[str, ...]
    adapter_forbidden: tuple[str, ...]
    budget: PlanningBudgetPolicy
    retry_policy: PlanningRetryPolicy
    default_priority: int
    execution_max_attempts: int
    execution_budget_level: str
    planner_secret_refs: tuple[str, ...]
    planner_capabilities: PlannerAdapterCapabilityProfile
    collaboration_runtime_binding: CollaborationRuntimeBindingSnapshot | None = None

    def __post_init__(self) -> None:
        if self.profile_version < 1 or self.planner_module_config_version < 1:
            raise ValidationError("profile and module versions must be positive")
        if self.proposal_schema_version < 1:
            raise ValidationError("proposal_schema_version must be positive")
        _require_text(self.profile_key, "profile_key")
        _require_text(self.display_name, "display_name")
        _require_text(self.planner_interface_version, "planner_interface_version")
        _require_text(self.prompt_policy_version, "prompt_policy_version")
        _require_text(self.execution_budget_level, "execution_budget_level")
        if not 0 <= self.default_priority <= 100:
            raise ValidationError("default_priority must be between 0 and 100")
        _positive(self.execution_max_attempts, "execution_max_attempts")
        object.__setattr__(
            self,
            "system_policy",
            _deep_freeze_mapping(self.system_policy, "system_policy"),
        )
        object.__setattr__(
            self,
            "allowed_capabilities",
            _text_tuple(self.allowed_capabilities, "allowed_capabilities"),
        )
        object.__setattr__(
            self,
            "adapter_preferences",
            _text_tuple(self.adapter_preferences, "adapter_preferences"),
        )
        object.__setattr__(
            self,
            "adapter_forbidden",
            _text_tuple(self.adapter_forbidden, "adapter_forbidden"),
        )
        object.__setattr__(
            self,
            "planner_secret_refs",
            _text_tuple(self.planner_secret_refs, "planner_secret_refs"),
        )
        _validate_collaboration_binding(
            self.collaboration_runtime_binding,
            proposal_schema_version=self.proposal_schema_version,
        )


@dataclass(frozen=True, slots=True)
class PlanningProfile:
    id: UUID
    workspace_id: UUID
    profile_key: str
    version: int
    display_name: str
    planner_module_instance_id: UUID
    planner_module_config_version: int
    planner_interface_version: str
    prompt_policy_version: str
    system_policy: Mapping[str, object]
    proposal_schema_version: int
    allowed_capabilities: tuple[str, ...]
    adapter_preferences: tuple[str, ...]
    adapter_forbidden: tuple[str, ...]
    budget: PlanningBudgetPolicy
    retry_policy: PlanningRetryPolicy
    default_priority: int
    execution_max_attempts: int
    execution_budget_level: str
    user_selectable: bool
    status: PlanningProfileStatus
    planner_secret_refs: tuple[str, ...]
    collaboration_runtime_binding: CollaborationRuntimeBindingSnapshot | None = None

    def __post_init__(self) -> None:
        _require_text(self.profile_key, "profile_key")
        _require_text(self.display_name, "display_name")
        _require_text(self.planner_interface_version, "planner_interface_version")
        _require_text(self.prompt_policy_version, "prompt_policy_version")
        _require_text(self.execution_budget_level, "execution_budget_level")
        if self.version < 1:
            raise ValidationError("profile version must be positive")
        _positive(self.planner_module_config_version, "planner_module_config_version")
        if self.proposal_schema_version < 1:
            raise ValidationError("proposal_schema_version must be positive")
        if not isinstance(self.user_selectable, bool):
            raise ValidationError("user_selectable must be a boolean")
        if not 0 <= self.default_priority <= 100:
            raise ValidationError("default_priority must be between 0 and 100")
        _positive(self.execution_max_attempts, "execution_max_attempts")
        object.__setattr__(
            self,
            "status",
            _enum_value(self.status, PlanningProfileStatus, "planning profile status"),
        )
        object.__setattr__(
            self,
            "system_policy",
            _deep_freeze_mapping(self.system_policy, "system_policy"),
        )
        object.__setattr__(
            self,
            "allowed_capabilities",
            _text_tuple(self.allowed_capabilities, "allowed_capabilities"),
        )
        object.__setattr__(
            self,
            "adapter_preferences",
            _text_tuple(self.adapter_preferences, "adapter_preferences"),
        )
        object.__setattr__(
            self,
            "adapter_forbidden",
            _text_tuple(self.adapter_forbidden, "adapter_forbidden"),
        )
        object.__setattr__(
            self,
            "planner_secret_refs",
            _text_tuple(self.planner_secret_refs, "planner_secret_refs"),
        )
        _validate_collaboration_binding(
            self.collaboration_runtime_binding,
            proposal_schema_version=self.proposal_schema_version,
        )

    def assert_user_selectable(self) -> None:
        if self.status is not PlanningProfileStatus.ACTIVE:
            raise ValidationError("planning profile is disabled")
        if not self.user_selectable:
            raise ValidationError("planning profile is not user-selectable")

    def snapshot(
        self,
        planner_capabilities: PlannerAdapterCapabilityProfile,
        *,
        module_type: str = PLANNER_ADAPTER_MODULE_TYPE,
        module_instance_config_version: int | None = None,
    ) -> PlanningProfileSnapshot:
        if not isinstance(planner_capabilities, PlannerAdapterCapabilityProfile):
            raise ValidationError("planner capabilities are required")
        if canonical_module_type(module_type) != PLANNER_ADAPTER_MODULE_TYPE:
            raise ValidationError("planning profile module type must be planner_adapter")
        config_version = (
            self.planner_module_config_version
            if module_instance_config_version is None
            else module_instance_config_version
        )
        if config_version != self.planner_module_config_version:
            raise ValidationError("planner module config version does not match profile")
        if planner_capabilities.interface_version != self.planner_interface_version:
            raise ValidationError("planner interface version does not match profile")
        if self.proposal_schema_version not in planner_capabilities.proposal_schema_versions:
            raise ValidationError("planner proposal schema version is not supported")
        if planner_capabilities.max_context_bytes < self.budget.max_context_bytes:
            raise ValidationError("planner context limit is below profile context budget")
        return PlanningProfileSnapshot(
            profile_id=self.id,
            workspace_id=self.workspace_id,
            profile_key=self.profile_key,
            profile_version=self.version,
            display_name=self.display_name,
            planner_module_instance_id=self.planner_module_instance_id,
            planner_module_config_version=self.planner_module_config_version,
            planner_interface_version=self.planner_interface_version,
            prompt_policy_version=self.prompt_policy_version,
            system_policy=_deep_freeze_mapping(self.system_policy, "system_policy"),
            proposal_schema_version=self.proposal_schema_version,
            allowed_capabilities=self.allowed_capabilities,
            adapter_preferences=self.adapter_preferences,
            adapter_forbidden=self.adapter_forbidden,
            budget=self.budget,
            retry_policy=self.retry_policy,
            default_priority=self.default_priority,
            execution_max_attempts=self.execution_max_attempts,
            execution_budget_level=self.execution_budget_level,
            planner_secret_refs=self.planner_secret_refs,
            planner_capabilities=planner_capabilities,
            collaboration_runtime_binding=self.collaboration_runtime_binding,
        )


def _validate_collaboration_binding(
    binding: CollaborationRuntimeBindingSnapshot | None,
    *,
    proposal_schema_version: int | None = None,
) -> None:
    if proposal_schema_version == 5:
        if binding is None:
            raise ValidationError(
                "proposal schema v5 requires a collaboration runtime binding"
            )
        if not binding.runtime_capability_snapshot.supports_input_groups_v1():
            raise ValidationError(
                "proposal schema v5 requires runtime input_groups_v1 capability"
            )
    if binding is None:
        return
    capability = binding.runtime_capability_snapshot
    limits = (
        ("projection", capability.max_projection_bytes, COLLABORATION_MAX_PROJECTION_BYTES),
        ("decision", capability.max_decision_bytes, COLLABORATION_MAX_DECISION_BYTES),
        ("intents", capability.max_intents_per_decision, COLLABORATION_MAX_INTENTS_PER_DECISION),
        ("strategy state", capability.max_strategy_state_bytes, COLLABORATION_MAX_STRATEGY_STATE_BYTES),
    )
    for label, declared, maximum in limits:
        if declared > maximum:
            raise ValidationError(
                f"global collaboration {label} limit is exceeded"
            )


def _enum_value(value: Any, enum_type: type[StrEnum], label: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {label}: {value!r}") from exc


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")


def _positive(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{name} must be positive")


def _positive_or_zero(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{name} must be zero or positive")


def _text_tuple(values: Sequence[str], name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValidationError(f"{name} must be a sequence of strings")
    clean: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{name} cannot contain empty values")
        item = value.strip()
        if item not in seen:
            seen.add(item)
            clean.append(item)
    return tuple(clean)


def _positive_tuple(
    values: Sequence[int], name: str, *, allow_empty: bool
) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{name} must be a sequence of integers")
    clean: list[int] = []
    seen: set[int] = set()
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{name} must contain positive integers")
        if value not in seen:
            seen.add(value)
            clean.append(value)
    if not allow_empty and not clean:
        raise ValidationError(f"{name} cannot be empty")
    return tuple(clean)


def _deep_freeze_mapping(
    value: Mapping[str, object], name: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    return MappingProxyType(
        {
            key: _deep_freeze(item, f"{name}.{key}")
            for key, item in value.items()
            if _validate_mapping_key(key, name)
        }
    )


def _deep_freeze(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return _deep_freeze_mapping(value, name)
    if isinstance(value, list):
        return tuple(_deep_freeze(item, name) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item, name) for item in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze(item, name) for item in value)
    return value


def _validate_mapping_key(key: object, name: str) -> bool:
    if not isinstance(key, str) or not key.strip():
        raise ValidationError(f"{name} keys must be non-empty strings")
    return True
