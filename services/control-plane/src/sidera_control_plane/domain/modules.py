from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError


class CapabilityInput(StrEnum):
    UNSUPPORTED = "unsupported"
    QUEUED = "queued"
    LIVE_SAFE_BOUNDARY = "live-safe-boundary"


class IdempotencyCapability(StrEnum):
    NONE = "none"
    OPERATION_ID = "operation-id"


class ResumeCapability(StrEnum):
    NONE = "none"
    SAME_PROCESS = "same-process"
    DURABLE = "durable"


class StopCapability(StrEnum):
    PROCESS = "process"
    SESSION = "session"
    EXACT_EXECUTION = "exact-execution"


class SandboxCapability(StrEnum):
    NONE = "none"
    FILESYSTEM = "filesystem"
    ISOLATED_RUNTIME = "isolated-runtime"


class StabilityLevel(StrEnum):
    EXPERIMENTAL = "experimental"
    STABLE = "stable"


class ModuleLifecycle(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    DISABLED = "disabled"


WORKER_ADAPTER_MODULE_TYPE = "worker_adapter"
WORKSPACE_PROVIDER_MODULE_TYPE = "workspace_provider"
PLANNER_ADAPTER_MODULE_TYPE = "planner_adapter"

_MODULE_TYPE_ALIASES = {
    "worker-adapter": WORKER_ADAPTER_MODULE_TYPE,
    "workspace-provider": WORKSPACE_PROVIDER_MODULE_TYPE,
    "planner-adapter": PLANNER_ADAPTER_MODULE_TYPE,
}


class StructuredOutputCapability(StrEnum):
    NONE = "none"
    JSON = "json"
    JSON_SCHEMA = "json_schema"


class CancellationCapability(StrEnum):
    NONE = "none"
    PROCESS = "process"
    RUN = "run"


class UsageReportingCapability(StrEnum):
    NONE = "none"
    TOKENS = "tokens"
    COST = "cost"


@dataclass(frozen=True, slots=True)
class WorkerAdapterCapabilityProfile:
    input: CapabilityInput
    idempotency: IdempotencyCapability
    resume: ResumeCapability
    stop: StopCapability
    sandbox: SandboxCapability
    stability: StabilityLevel
    context_package_versions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "input", _enum_value(self.input, CapabilityInput, "input capability")
        )
        object.__setattr__(
            self,
            "idempotency",
            _enum_value(
                self.idempotency,
                IdempotencyCapability,
                "idempotency capability",
            ),
        )
        object.__setattr__(
            self,
            "resume",
            _enum_value(self.resume, ResumeCapability, "resume capability"),
        )
        object.__setattr__(
            self,
            "stop",
            _enum_value(self.stop, StopCapability, "stop capability"),
        )
        object.__setattr__(
            self,
            "sandbox",
            _enum_value(self.sandbox, SandboxCapability, "sandbox capability"),
        )
        object.__setattr__(
            self,
            "stability",
            _enum_value(self.stability, StabilityLevel, "stability level"),
        )
        object.__setattr__(
            self,
            "context_package_versions",
            _positive_version_tuple(
                self.context_package_versions, "context_package_versions"
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceProviderCapabilityProfile:
    isolation: SandboxCapability
    writable: bool
    git_worktree: bool
    stability: StabilityLevel

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "isolation",
            _enum_value(self.isolation, SandboxCapability, "isolation capability"),
        )
        if not isinstance(self.writable, bool):
            raise ValidationError("writable capability must be a boolean")
        if not isinstance(self.git_worktree, bool):
            raise ValidationError("git_worktree capability must be a boolean")
        object.__setattr__(
            self,
            "stability",
            _enum_value(self.stability, StabilityLevel, "stability level"),
        )


@dataclass(frozen=True, slots=True)
class PlannerAdapterCapabilityProfile:
    structured_output: StructuredOutputCapability
    clarification: bool
    idempotency: IdempotencyCapability
    cancellation: CancellationCapability
    usage_reporting: UsageReportingCapability
    max_context_bytes: int
    stability: StabilityLevel
    interface_version: str
    proposal_schema_versions: tuple[int, ...]
    context_package_versions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "structured_output",
            _enum_value(
                self.structured_output,
                StructuredOutputCapability,
                "structured output capability",
            ),
        )
        if not isinstance(self.clarification, bool):
            raise ValidationError("clarification capability must be a boolean")
        object.__setattr__(
            self,
            "idempotency",
            _enum_value(
                self.idempotency,
                IdempotencyCapability,
                "idempotency capability",
            ),
        )
        object.__setattr__(
            self,
            "cancellation",
            _enum_value(
                self.cancellation,
                CancellationCapability,
                "cancellation capability",
            ),
        )
        object.__setattr__(
            self,
            "usage_reporting",
            _enum_value(
                self.usage_reporting,
                UsageReportingCapability,
                "usage reporting capability",
            ),
        )
        if (
            not isinstance(self.max_context_bytes, int)
            or isinstance(self.max_context_bytes, bool)
            or self.max_context_bytes <= 0
        ):
            raise ValidationError("max_context_bytes must be positive")
        object.__setattr__(
            self,
            "stability",
            _enum_value(self.stability, StabilityLevel, "stability level"),
        )
        _require_text(self.interface_version, "interface_version")
        if not isinstance(self.proposal_schema_versions, tuple):
            raise ValidationError("proposal_schema_versions must be a tuple")
        if not self.proposal_schema_versions:
            raise ValidationError("proposal_schema_versions cannot be empty")
        if any(
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
            for version in self.proposal_schema_versions
        ):
            raise ValidationError("proposal_schema_versions must be positive integers")
        object.__setattr__(
            self,
            "proposal_schema_versions",
            tuple(sorted(set(self.proposal_schema_versions))),
        )
        object.__setattr__(
            self,
            "context_package_versions",
            _positive_version_tuple(
                self.context_package_versions, "context_package_versions"
            ),
        )


type ModuleCapabilityProfile = (
    WorkerAdapterCapabilityProfile
    | WorkspaceProviderCapabilityProfile
    | PlannerAdapterCapabilityProfile
)

# Temporary source compatibility for existing WorkerAdapter implementations.
CapabilityProfile = WorkerAdapterCapabilityProfile


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    id: UUID
    workspace_id: UUID
    module_type: str
    provider: str
    interface_version: str
    config_schema_version: int
    capabilities: ModuleCapabilityProfile
    lifecycle: ModuleLifecycle
    resource_version: int = 1

    def __post_init__(self) -> None:
        module_type = canonical_module_type(self.module_type)
        object.__setattr__(self, "module_type", module_type)
        _require_text(self.provider, "provider")
        _require_text(self.interface_version, "interface_version")
        if self.config_schema_version < 1:
            raise ValidationError("config_schema_version must be positive")
        _require_resource_version(self.resource_version)
        expected_profile = _profile_type_for_module(module_type)
        if not isinstance(self.capabilities, expected_profile):
            raise ValidationError(
                f"{module_type} requires {expected_profile.__name__} capabilities"
            )
        object.__setattr__(
            self,
            "lifecycle",
            _enum_value(self.lifecycle, ModuleLifecycle, "module lifecycle"),
        )

    def with_lifecycle(self, lifecycle: ModuleLifecycle | str) -> "ModuleDefinition":
        target = _module_lifecycle(lifecycle)
        _validate_lifecycle_transition(self.lifecycle, target)
        return replace(
            self,
            lifecycle=target,
            resource_version=self.resource_version + 1,
        )


@dataclass(frozen=True, slots=True)
class ModuleInstance:
    id: UUID
    workspace_id: UUID
    definition_id: UUID
    instance_name: str
    config_version: int
    config_namespace: str
    config: Mapping[str, Any]
    secret_refs: tuple[str, ...]
    lifecycle: ModuleLifecycle
    resource_version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.instance_name, "instance_name")
        namespace = self.config_namespace.strip()
        if not namespace:
            raise ValidationError("config namespace is required")
        if self.config_version < 1:
            raise ValidationError("config_version must be positive")
        _require_resource_version(self.resource_version)
        if not isinstance(self.config, Mapping):
            raise ValidationError("config must be a mapping")
        prefix = f"{namespace}."
        if any(not str(key).startswith(prefix) for key in self.config):
            raise ValidationError("config keys must use the configured namespace")
        if any(not item.strip() for item in self.secret_refs):
            raise ValidationError("secret_refs cannot contain empty values")
        object.__setattr__(self, "config_namespace", namespace)
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))
        object.__setattr__(
            self,
            "secret_refs",
            tuple(item.strip() for item in self.secret_refs),
        )
        object.__setattr__(
            self,
            "lifecycle",
            _enum_value(self.lifecycle, ModuleLifecycle, "module lifecycle"),
        )

    def with_lifecycle(self, lifecycle: ModuleLifecycle | str) -> "ModuleInstance":
        target = _module_lifecycle(lifecycle)
        _validate_lifecycle_transition(self.lifecycle, target)
        return replace(
            self,
            lifecycle=target,
            resource_version=self.resource_version + 1,
        )


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    worker_id: UUID
    adapter_module_id: UUID
    workspace_module_id: UUID
    adapter_config_version: int
    workspace_config_version: int
    capability_snapshot: WorkerAdapterCapabilityProfile
    lease_id: UUID
    fencing_token: int
    version: int

    def __post_init__(self) -> None:
        if self.adapter_config_version < 1 or self.workspace_config_version < 1:
            raise ValidationError("binding config versions must be positive")
        if self.fencing_token < 1 or self.version < 1:
            raise ValidationError("binding fencing token and version must be positive")
        if not isinstance(self.capability_snapshot, WorkerAdapterCapabilityProfile):
            raise ValidationError(
                "binding capability_snapshot requires WorkerAdapter capabilities"
            )


def canonical_module_type(value: str) -> str:
    _require_text(value, "module_type")
    canonical = _MODULE_TYPE_ALIASES.get(value.strip(), value.strip())
    if canonical not in {
        WORKER_ADAPTER_MODULE_TYPE,
        WORKSPACE_PROVIDER_MODULE_TYPE,
        PLANNER_ADAPTER_MODULE_TYPE,
    }:
        raise ValidationError(f"unsupported module_type: {value!r}")
    return canonical


def _profile_type_for_module(module_type: str) -> type[object]:
    if module_type == WORKER_ADAPTER_MODULE_TYPE:
        return WorkerAdapterCapabilityProfile
    if module_type == WORKSPACE_PROVIDER_MODULE_TYPE:
        return WorkspaceProviderCapabilityProfile
    return PlannerAdapterCapabilityProfile


def _module_lifecycle(value: ModuleLifecycle | str) -> ModuleLifecycle:
    try:
        return value if isinstance(value, ModuleLifecycle) else ModuleLifecycle(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported module lifecycle: {value!r}") from exc


def _validate_lifecycle_transition(
    current: ModuleLifecycle,
    target: ModuleLifecycle,
) -> None:
    if current is target:
        raise InvalidTransition("module lifecycle is unchanged")
    if current is ModuleLifecycle.DISABLED:
        raise InvalidTransition("disabled module lifecycle cannot change")
    if (current, target) not in {
        (ModuleLifecycle.ACTIVE, ModuleLifecycle.DRAINING),
        (ModuleLifecycle.ACTIVE, ModuleLifecycle.DISABLED),
        (ModuleLifecycle.DRAINING, ModuleLifecycle.ACTIVE),
        (ModuleLifecycle.DRAINING, ModuleLifecycle.DISABLED),
    }:
        raise InvalidTransition(
            f"module lifecycle cannot change from {current} to {target}"
        )


def _require_resource_version(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError("resource_version must be a positive integer")


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


def _positive_version_tuple(value: tuple[int, ...], name: str) -> tuple[int, ...]:
    if not isinstance(value, tuple):
        raise ValidationError(f"{name} must be a tuple")
    if any(
        not isinstance(version, int) or isinstance(version, bool) or version < 1
        for version in value
    ):
        raise ValidationError(f"{name} must contain positive integers")
    return tuple(sorted(set(value)))
