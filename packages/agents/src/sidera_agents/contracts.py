from __future__ import annotations

import math
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
)


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime must use UTC")
    return value


def _canonical_json_copy(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, (list, tuple)):
        return [_canonical_json_copy(item) for item in value]
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _canonical_json_copy(value[key]) for key in sorted(value)}
    raise ValueError("value must contain only JSON values")


def _canonical_projected_copy(value: object) -> object:
    if isinstance(value, SecretStr):
        return value
    if isinstance(value, list):
        return [_canonical_projected_copy(item) for item in value]
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise ValueError("projected object keys must be strings")
        return {key: _canonical_projected_copy(value[key]) for key in sorted(value)}
    return _canonical_json_copy(value)


def _copy_string_map(value: object) -> object:
    if not isinstance(value, dict):
        return value
    if any(type(key) is not str for key in value):
        raise ValueError("mapping keys must be strings")
    return {key: value[key] for key in sorted(value)}


type JsonValue = (
    None
    | StrictBool
    | StrictInt
    | Annotated[float, Field(strict=True, allow_inf_nan=False)]
    | StrictStr
    | list["JsonValue"]
    | dict[StrictStr, "JsonValue"]
)

type _ProjectedJsonValue = (
    None
    | StrictBool
    | StrictInt
    | Annotated[float, Field(strict=True, allow_inf_nan=False)]
    | StrictStr
    | SecretStr
    | list["_ProjectedJsonValue"]
    | dict[StrictStr, "_ProjectedJsonValue"]
)

UTCDateTime = Annotated[AwareDatetime, AfterValidator(_require_utc)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
ContractVersion = Annotated[int, Field(strict=True, ge=1, le=1)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceKind(StrEnum):
    SKILL = "skill"
    MCP = "mcp"
    INTEGRATION = "integration"


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNCONFIRMED = "unconfirmed"


class ProtocolState(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    UNCONFIRMED = "unconfirmed"


class AuthenticationState(StrEnum):
    READY = "ready"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"


class ConfigFieldKind(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    SELECT = "select"
    TOGGLE = "toggle"


class RunPolicy(StrEnum):
    BOUNDED = "bounded"
    CONVERSATION = "conversation"


class StartStatus(StrEnum):
    ACCEPTED = "accepted"
    BUSY = "busy"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class RunState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    CLOSING = "closing"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class InputStatus(StrEnum):
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    FAILED = "failed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class InterruptStatus(StrEnum):
    ACCEPTED = "accepted"
    INTERRUPTED = "interrupted"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    UNKNOWN = "unknown"


class StopStatus(StrEnum):
    ACCEPTED = "accepted"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RecoveryStatus(StrEnum):
    RECOVERED = "recovered"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    FAILED = "failed"


class ProfileRef(ContractModel):
    workspace_id: UUID
    profile_id: UUID
    revision: PositiveInt


class RuntimeRef(ContractModel):
    workspace_id: UUID
    runtime_id: UUID


class ResourceRef(ContractModel):
    workspace_id: UUID
    resource_id: UUID
    revision: PositiveInt


class CredentialRef(ContractModel):
    workspace_id: UUID
    credential_id: UUID


class SecretBinding(ContractModel):
    credential_ref: CredentialRef
    destination: str


class CapabilitySupport(ContractModel):
    status: SupportStatus
    reason_code: str
    constraints: dict[str, JsonValue]

    @classmethod
    def _canonicalize_constraints(cls, value: object) -> object:
        return _canonical_json_copy(value)

    from pydantic import field_validator as _field_validator

    _validate_constraints = _field_validator("constraints", mode="before")(
        _canonicalize_constraints
    )


class ProfileDraft(ContractModel):
    name: str
    description: str
    runtime_ref: RuntimeRef
    instructions: str
    resource_refs: tuple[ResourceRef, ...]
    model_id: str | None
    thinking: str | None
    max_active: PositiveInt
    environment: dict[str, str]
    secret_refs: tuple[SecretBinding, ...]
    native_settings: dict[str, JsonValue]

    from pydantic import field_validator as _field_validator

    _copy_environment = _field_validator("environment", mode="before")(_copy_string_map)
    _canonicalize_native_settings = _field_validator("native_settings", mode="before")(
        _canonical_json_copy
    )


class ProfileRevision(ContractModel):
    ref: ProfileRef
    draft: ProfileDraft
    config_hash: str
    created_at: UTCDateTime


class CapabilityResourceDraft(ContractModel):
    name: str
    kind: ResourceKind
    settings: dict[str, JsonValue]
    secret_refs: tuple[SecretBinding, ...]

    from pydantic import field_validator as _field_validator

    _canonicalize_settings = _field_validator("settings", mode="before")(
        _canonical_json_copy
    )


class RuntimeSnapshot(ContractModel):
    ref: RuntimeRef
    harness_id: str
    executable: str
    actual_version: str
    installed: StrictBool
    enabled: StrictBool
    version: PositiveInt
    protocol_state: ProtocolState
    auth_state: AuthenticationState
    capabilities: dict[str, CapabilitySupport]
    checked_at: UTCDateTime
    limits: dict[str, PositiveInt]

    from pydantic import field_validator as _field_validator

    _copy_capabilities = _field_validator("capabilities", mode="before")(
        _copy_string_map
    )
    _copy_limits = _field_validator("limits", mode="before")(_copy_string_map)


class ConfigField(ContractModel):
    key: str
    label: str
    kind: ConfigFieldKind
    options: tuple[str, ...]
    minimum: float | None
    maximum: float | None
    support: CapabilitySupport


class RuntimeDescriptor(ContractModel):
    harness_id: str
    display_name: str
    field_schema_version: ContractVersion = 1
    fields: tuple[ConfigField, ...]
    model_options: tuple[str, ...]
    thinking_options: tuple[str, ...]


class RuntimeCandidate(ContractModel):
    harness_id: str
    executable: str


class SourceRef(ContractModel):
    provider: str
    key: str
    version: str


class ContextItem(ContractModel):
    item_id: str
    origin: str
    required: StrictBool
    ordinal: NonNegativeInt
    version: str
    content_hash: str
    text: str | None = None
    source_ref: SourceRef | None = None

    from pydantic import model_validator as _model_validator

    @_model_validator(mode="after")
    def _validate_source(self) -> ContextItem:
        if (self.text is None) == (self.source_ref is None):
            raise ValueError("exactly one of text or source_ref is required")
        if self.source_ref is not None and self.source_ref.version != self.version:
            raise ValueError("source_ref version must match context item version")
        return self


class SourceContent(ContractModel):
    text: str
    actual_version: str
    content_hash: str


class ToolBinding(ContractModel):
    resource_ref: ResourceRef | None = None
    connection_id: UUID | None = None

    from pydantic import model_validator as _model_validator

    @_model_validator(mode="after")
    def _validate_target(self) -> ToolBinding:
        if (self.resource_ref is None) == (self.connection_id is None):
            raise ValueError("exactly one of resource_ref or connection_id is required")
        return self


class ToolConnection(ContractModel):
    name: str
    transport: str
    endpoint_or_argv: str | tuple[str, ...]
    headers: dict[str, SecretStr] = Field(exclude=True, repr=False)
    environment: dict[str, SecretStr] = Field(exclude=True, repr=False)

    from pydantic import field_validator as _field_validator

    _copy_headers = _field_validator("headers", mode="before")(_copy_string_map)
    _copy_environment = _field_validator("environment", mode="before")(_copy_string_map)


class NativeSkill(ContractModel):
    resource_ref: ResourceRef
    name: StrictStr
    content: StrictStr = Field(repr=False)
    content_hash: StrictStr

    from pydantic import field_validator as _field_validator

    @_field_validator("content_hash")
    @classmethod
    def _validate_content_hash(cls, value: str) -> str:
        if len(value) != 71 or not value.startswith("sha256:"):
            raise ValueError("content_hash must be a SHA-256 digest")
        try:
            bytes.fromhex(value[7:])
        except ValueError as error:
            raise ValueError("content_hash must be a SHA-256 digest") from error
        return value

    from pydantic import model_validator as _model_validator

    @_model_validator(mode="after")
    def _verify_content_hash(self) -> NativeSkill:
        import hashlib

        expected = "sha256:" + hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_hash != expected:
            raise ValueError("content_hash does not match skill content")
        if not self.name.strip() or "\x00" in self.name:
            raise ValueError("skill name must be non-empty text")
        return self


class ExecutionWorkspace(ContractModel):
    directory: str
    writable: StrictBool
    artifact_directory: str | None


class RunStart(ContractModel):
    contract_version: ContractVersion = 1
    operation_id: UUID
    profile_ref: ProfileRef
    grant_id: UUID
    policy: RunPolicy
    instructions: tuple[str, ...]
    text: str
    context: tuple[ContextItem, ...]
    tools: tuple[ToolBinding, ...]
    workspace: ExecutionWorkspace
    required_capabilities: tuple[str, ...]


class StartReceipt(ContractModel):
    operation_id: UUID
    status: StartStatus
    run_id: UUID | None
    reason_code: str | None

    from pydantic import model_validator as _model_validator

    @_model_validator(mode="after")
    def _accepted_requires_run(self) -> StartReceipt:
        if self.status is StartStatus.ACCEPTED and self.run_id is None:
            raise ValueError("accepted start receipt requires run_id")
        return self


class StartLookup(ContractModel):
    found: StrictBool
    receipt: StartReceipt | None


class EffectiveConfig(ContractModel):
    profile_ref: ProfileRef
    config_hash: str
    runtime_ref: RuntimeRef
    actual_runtime_version: str
    selected_model: str | None
    observed_model: str | None
    inherited_fields: tuple[str, ...]
    applied_fields: tuple[str, ...]
    resource_refs: tuple[ResourceRef, ...]
    context_receipts: tuple[dict[str, JsonValue], ...]

    from pydantic import field_validator as _field_validator

    _canonicalize_receipts = _field_validator("context_receipts", mode="before")(
        _canonical_json_copy
    )


class RunSnapshot(ContractModel):
    run_id: UUID
    workspace_id: UUID
    profile_ref: ProfileRef
    policy: RunPolicy
    state: RunState
    input_open: StrictBool
    active_response_id: UUID | None
    effective_config: EffectiveConfig
    last_event_sequence: NonNegativeInt
    capabilities: dict[str, CapabilitySupport]
    reason_code: str | None

    from pydantic import field_validator as _field_validator

    _copy_capabilities = _field_validator("capabilities", mode="before")(
        _copy_string_map
    )


class RunInput(ContractModel):
    workspace_id: UUID
    run_id: UUID
    operation_id: UUID
    grant_id: UUID
    text: str
    context: tuple[ContextItem, ...]


class InputReceipt(ContractModel):
    operation_id: UUID
    run_id: UUID
    status: InputStatus
    reason_code: str | None
    evidence_sequence: PositiveInt | None


class RunInterrupt(ContractModel):
    workspace_id: UUID
    run_id: UUID
    operation_id: UUID
    grant_id: UUID
    response_id: UUID


class InterruptReceipt(ContractModel):
    operation_id: UUID
    run_id: UUID
    response_id: UUID
    status: InterruptStatus
    reason_code: str | None
    evidence_sequence: PositiveInt | None


class RunStop(ContractModel):
    workspace_id: UUID
    run_id: UUID
    operation_id: UUID
    grant_id: UUID


class RunRecover(ContractModel):
    workspace_id: UUID
    run_id: UUID
    operation_id: UUID
    grant_id: UUID


class StopReceipt(ContractModel):
    operation_id: UUID
    run_id: UUID
    status: StopStatus
    reason_code: str | None
    evidence_sequence: PositiveInt | None


class RecoveryReceipt(ContractModel):
    operation_id: UUID
    run_id: UUID
    status: RecoveryStatus
    reason_code: str | None


class RunEvent(ContractModel):
    run_id: UUID
    sequence: PositiveInt
    kind: str
    occurred_at: UTCDateTime
    operation_id: UUID | None
    response_id: UUID | None
    data: dict[str, JsonValue]

    from pydantic import field_validator as _field_validator

    _canonicalize_data = _field_validator("data", mode="before")(_canonical_json_copy)


class EventPage(ContractModel):
    events: tuple[RunEvent, ...]
    next_sequence: NonNegativeInt
    has_more: StrictBool


class OutputRef(ContractModel):
    ref_id: str
    media_type: str
    uri: str
    content_hash: str


class ResourceRevision(ContractModel):
    ref: ResourceRef
    draft: CapabilityResourceDraft
    content_hash: str
    created_at: UTCDateTime


class ProfileStatus(ContractModel):
    profile_id: UUID
    enabled: StrictBool
    version: PositiveInt


class ResourceStatus(ContractModel):
    resource_id: UUID
    enabled: StrictBool
    version: PositiveInt


class ConfigIssue(ContractModel):
    field: str
    code: str
    message: str
    support: CapabilitySupport


class ConfigValidation(ContractModel):
    valid: StrictBool
    issues: tuple[ConfigIssue, ...]
    effective_preview: EffectiveConfig | None


class NativeOutcomeStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


class NativeCancelState(StrEnum):
    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    UNKNOWN = "unknown"


class NativeProcessState(StrEnum):
    ALIVE = "alive"
    GONE = "gone"
    UNKNOWN = "unknown"


class NativeResponseState(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    UNKNOWN = "unknown"


class NativeCloseState(StrEnum):
    CLOSED = "closed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RuntimeProbe(ContractModel):
    actual_version: str
    protocol_state: ProtocolState
    capabilities: dict[str, CapabilitySupport]
    checked_at: UTCDateTime

    from pydantic import field_validator as _field_validator

    _copy_capabilities = _field_validator("capabilities", mode="before")(
        _copy_string_map
    )


class ProcessIdentity(ContractModel):
    pid: PositiveInt
    start_identity: str
    owned_nonce: UUID


class NativeHandle(ContractModel):
    run_id: UUID
    session_id: str | None
    process_identity: ProcessIdentity
    capabilities: tuple[str, ...]


class AssembledInput(ContractModel):
    instructions: tuple[str, ...]
    text: str
    context: tuple[ContextItem, ...]


class NativeOpenRequest(ContractModel):
    run_id: UUID
    runtime: RuntimeSnapshot
    projected_config: dict[str, _ProjectedJsonValue] = Field(exclude=True, repr=False)
    workspace: ExecutionWorkspace
    tools: tuple[ToolConnection, ...] = Field(exclude=True, repr=False)
    skills: tuple[NativeSkill, ...] = Field(default=(), exclude=True, repr=False)
    environment: dict[str, _ProjectedJsonValue] = Field(exclude=True, repr=False)

    from pydantic import field_validator as _field_validator

    _canonicalize_projected_config = _field_validator(
        "projected_config", mode="before"
    )(_canonical_projected_copy)
    _canonicalize_environment = _field_validator("environment", mode="before")(
        _canonical_projected_copy
    )


class NativeEvent(ContractModel):
    cursor: NonNegativeInt
    native_id: str | None
    kind: str
    response_id: UUID | None
    operation_id: UUID | None
    occurred_at: UTCDateTime
    data: dict[str, JsonValue]

    from pydantic import field_validator as _field_validator

    _canonicalize_data = _field_validator("data", mode="before")(_canonical_json_copy)


class NativeOutcome(ContractModel):
    response_id: UUID
    status: NativeOutcomeStatus
    reason_code: str | None
    observed_model: str | None
    usage: dict[str, JsonValue] | None

    from pydantic import field_validator as _field_validator

    _canonicalize_usage = _field_validator("usage", mode="before")(
        lambda cls, value: None if value is None else _canonical_json_copy(value)
    )


class NativeCancelOutcome(ContractModel):
    response_id: UUID
    status: NativeCancelState
    reason_code: str | None


class NativeInspection(ContractModel):
    process_state: NativeProcessState
    response_state: NativeResponseState
    reason_code: str | None


class NativeCloseOutcome(ContractModel):
    status: NativeCloseState
    reason_code: str | None


class HarnessAdapter(Protocol):
    def describe(self) -> RuntimeDescriptor: ...

    def probe(self, candidate: RuntimeCandidate) -> RuntimeProbe: ...

    def validate(
        self, profile: ProfileRevision, runtime: RuntimeSnapshot
    ) -> tuple[ConfigIssue, ...]: ...

    def open(self, request: NativeOpenRequest) -> NativeHandle: ...

    def prompt(
        self, handle: NativeHandle, input: AssembledInput, response_id: UUID
    ) -> NativeOutcome: ...

    def read_events(
        self, handle: NativeHandle, after_cursor: int, limit: int = 100
    ) -> tuple[NativeEvent, ...]: ...

    def cancel(
        self, handle: NativeHandle, response_id: UUID
    ) -> NativeCancelOutcome: ...

    def inspect(self, handle: NativeHandle) -> NativeInspection: ...

    def close(self, handle: NativeHandle) -> NativeCloseOutcome: ...
