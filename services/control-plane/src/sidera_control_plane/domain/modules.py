from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError


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


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    input: CapabilityInput
    idempotency: IdempotencyCapability
    resume: ResumeCapability
    stop: StopCapability
    sandbox: SandboxCapability
    stability: StabilityLevel

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


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    id: UUID
    workspace_id: UUID
    module_type: str
    provider: str
    interface_version: str
    config_schema_version: int
    capabilities: CapabilityProfile
    lifecycle: ModuleLifecycle

    def __post_init__(self) -> None:
        _require_text(self.module_type, "module_type")
        _require_text(self.provider, "provider")
        _require_text(self.interface_version, "interface_version")
        if self.config_schema_version < 1:
            raise ValidationError("config_schema_version must be positive")
        if not isinstance(self.capabilities, CapabilityProfile):
            raise ValidationError("capabilities must be a CapabilityProfile")
        object.__setattr__(
            self,
            "lifecycle",
            _enum_value(self.lifecycle, ModuleLifecycle, "module lifecycle"),
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

    def __post_init__(self) -> None:
        _require_text(self.instance_name, "instance_name")
        namespace = self.config_namespace.strip()
        if not namespace:
            raise ValidationError("config namespace is required")
        if self.config_version < 1:
            raise ValidationError("config_version must be positive")
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
    capability_snapshot: CapabilityProfile
    lease_id: UUID
    fencing_token: int
    version: int

    def __post_init__(self) -> None:
        if self.adapter_config_version < 1 or self.workspace_config_version < 1:
            raise ValidationError("binding config versions must be positive")
        if self.fencing_token < 1 or self.version < 1:
            raise ValidationError("binding fencing token and version must be positive")
        if not isinstance(self.capability_snapshot, CapabilityProfile):
            raise ValidationError("binding capability_snapshot is required")


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
