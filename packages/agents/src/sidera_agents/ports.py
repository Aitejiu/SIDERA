from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, Protocol
from uuid import UUID

from pydantic import SecretStr

from .contracts import (
    AssembledInput,
    AuthenticationState,
    ConfigValidation,
    NativeCancelOutcome,
    NativeCloseOutcome,
    NativeEvent,
    NativeHandle,
    NativeInspection,
    NativeOpenRequest,
    NativeOutcome,
    ProfileRef,
    ProfileRevision,
    ProfileStatus,
    ResourceRef,
    ResourceRevision,
    ResourceStatus,
    RuntimeCandidate,
    RuntimeDescriptor,
    RuntimeProbe,
    RuntimeRef,
    RuntimeSnapshot,
    SecretBinding,
    SourceContent,
    SourceRef,
    ToolConnection,
)
from .errors import AgentError


class ExecutionAuthorizer(Protocol):
    def require(self, grant_id: UUID, action: str, target: object) -> None: ...


class ContextSourceReader(Protocol):
    def read(self, grant_id: UUID, ref: SourceRef) -> SourceContent: ...


class SecretReader(Protocol):
    def read(self, grant_id: UUID, binding: SecretBinding) -> SecretStr: ...


class ToolConnectionReader(Protocol):
    def resolve(self, grant_id: UUID, connection_id: UUID) -> ToolConnection: ...


class HarnessAdapter(Protocol):
    """Native boundary for explicitly trusted, statically composed adapters."""

    harness_id: str
    executable_name: str

    def describe(self) -> RuntimeDescriptor: ...

    def probe(
        self, candidate: RuntimeCandidate, checked_at: datetime
    ) -> RuntimeProbe: ...

    def validate(
        self, profile: ProfileRevision, runtime: RuntimeSnapshot
    ) -> ConfigValidation: ...

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


@dataclass(frozen=True)
class RuntimeObservation:
    probe: RuntimeProbe
    installed: bool
    file_identity: str | None
    auth_state: AuthenticationState = AuthenticationState.UNCONFIRMED


@dataclass(frozen=True)
class RuntimeInstallation:
    installation_key: str
    harness_id: str
    executable: str
    max_active: int
    max_sessions: int
    enabled: bool
    version: int
    active_count: int = 0
    session_count: int = 0


@dataclass(frozen=True)
class RuntimeRecord:
    ref: RuntimeRef
    installation_key: str
    harness_id: str
    executable: str
    observation: RuntimeObservation


@dataclass(frozen=True)
class RuntimeEntry:
    record: RuntimeRecord
    installation: RuntimeInstallation


class RuntimeStore(Protocol):
    def find_runtime(
        self, workspace_id: UUID, harness_id: str, executable: str
    ) -> RuntimeEntry | None: ...

    def register_runtime(
        self,
        workspace_id: UUID,
        runtime_id: UUID,
        harness_id: str,
        executable: str,
        installation_key: str,
        observation: RuntimeObservation,
        max_active: int,
        max_sessions: int,
    ) -> RuntimeEntry: ...

    def get_runtime_entry(self, ref: RuntimeRef) -> RuntimeEntry: ...

    def list_runtime_entries(self, workspace_id: UUID) -> tuple[RuntimeEntry, ...]: ...

    def update_runtime_observation(
        self, ref: RuntimeRef, observation: RuntimeObservation
    ) -> RuntimeEntry: ...

    def set_runtime_configuration(
        self,
        ref: RuntimeRef,
        *,
        expected_version: int,
        max_active: int | None = None,
        max_sessions: int | None = None,
        enabled: bool | None = None,
    ) -> RuntimeEntry: ...

    def get_profile(self, ref: ProfileRef) -> ProfileRevision: ...

    def get_profile_status(
        self, workspace_id: UUID, profile_id: UUID
    ) -> ProfileStatus: ...

    def get_resource(self, ref: ResourceRef) -> ResourceRevision: ...

    def get_resource_status(
        self, workspace_id: UUID, resource_id: UUID
    ) -> ResourceStatus: ...


def _deny() -> NoReturn:
    raise AgentError("permission_denied", "Access denied.")


class DenyExecutionAuthorizer:
    def require(self, grant_id: UUID, action: str, target: object) -> None:
        _deny()


class DenyContextSourceReader:
    def read(self, grant_id: UUID, ref: SourceRef) -> SourceContent:
        _deny()


class DenySecretReader:
    def read(self, grant_id: UUID, binding: SecretBinding) -> SecretStr:
        _deny()


class DenyToolConnectionReader:
    def resolve(self, grant_id: UUID, connection_id: UUID) -> ToolConnection:
        _deny()
