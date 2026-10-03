from typing import Protocol
from uuid import UUID

from .contracts import (
    CapabilityResourceDraft,
    ConfigValidation,
    EventPage,
    InputReceipt,
    InterruptReceipt,
    OutputRef,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    ProfileStatus,
    RecoveryReceipt,
    ResourceRef,
    ResourceRevision,
    ResourceStatus,
    RunInput,
    RunInterrupt,
    RunRecover,
    RunSnapshot,
    RunStart,
    RunStop,
    RuntimeCandidate,
    RuntimeDescriptor,
    RuntimeRef,
    RuntimeSnapshot,
    StartLookup,
    StartReceipt,
    StopReceipt,
)


class AgentCatalog(Protocol):
    def create_profile(
        self, workspace_id: UUID, draft: ProfileDraft, *, grant_id: UUID
    ) -> ProfileRevision: ...

    def revise_profile(
        self, ref: ProfileRef, draft: ProfileDraft, *, grant_id: UUID
    ) -> ProfileRevision: ...

    def get_profile(self, ref: ProfileRef, *, grant_id: UUID) -> ProfileRevision: ...

    def get_profile_status(
        self, ref: ProfileRef, *, grant_id: UUID
    ) -> ProfileStatus: ...

    def list_profiles(
        self, workspace_id: UUID, *, grant_id: UUID
    ) -> tuple[ProfileRevision, ...]: ...

    def create_resource(
        self, workspace_id: UUID, draft: CapabilityResourceDraft, *, grant_id: UUID
    ) -> ResourceRevision: ...

    def revise_resource(
        self, ref: ResourceRef, draft: CapabilityResourceDraft, *, grant_id: UUID
    ) -> ResourceRevision: ...

    def get_resource(self, ref: ResourceRef, *, grant_id: UUID) -> ResourceRevision: ...

    def get_resource_status(
        self, ref: ResourceRef, *, grant_id: UUID
    ) -> ResourceStatus: ...

    def list_resources(
        self, workspace_id: UUID, *, grant_id: UUID
    ) -> tuple[ResourceRevision, ...]: ...

    def set_profile_enabled(
        self,
        workspace_id: UUID,
        profile_id: UUID,
        enabled: bool,
        expected_version: int,
        *,
        grant_id: UUID,
    ) -> ProfileStatus: ...

    def set_resource_enabled(
        self,
        workspace_id: UUID,
        resource_id: UUID,
        enabled: bool,
        expected_version: int,
        *,
        grant_id: UUID,
    ) -> ResourceStatus: ...


class AgentRuntimes(Protocol):
    def discover(
        self, workspace_id: UUID, *, grant_id: UUID
    ) -> tuple[RuntimeCandidate, ...]: ...

    def register(
        self,
        workspace_id: UUID,
        harness_id: str,
        executable: str,
        max_active: int,
        max_sessions: int,
        *,
        grant_id: UUID,
    ) -> RuntimeSnapshot: ...

    def refresh(self, ref: RuntimeRef, *, grant_id: UUID) -> RuntimeSnapshot: ...

    def get_runtime(self, ref: RuntimeRef, *, grant_id: UUID) -> RuntimeSnapshot: ...

    def list_runtimes(
        self, workspace_id: UUID, *, grant_id: UUID
    ) -> tuple[RuntimeSnapshot, ...]: ...

    def describe(self, harness_id: str, *, grant_id: UUID) -> RuntimeDescriptor: ...

    def validate_profile(
        self, ref: ProfileRef, *, grant_id: UUID
    ) -> ConfigValidation: ...

    def set_runtime_limits(
        self,
        ref: RuntimeRef,
        max_active: int,
        max_sessions: int,
        expected_version: int,
        *,
        grant_id: UUID,
    ) -> RuntimeSnapshot: ...

    def set_runtime_enabled(
        self,
        ref: RuntimeRef,
        enabled: bool,
        expected_version: int,
        *,
        grant_id: UUID,
    ) -> RuntimeSnapshot: ...


class AgentExecution(Protocol):
    def start(self, request: RunStart) -> StartReceipt: ...

    def get_start(
        self, workspace_id: UUID, operation_id: UUID, *, grant_id: UUID
    ) -> StartLookup: ...

    def get_run(
        self, workspace_id: UUID, run_id: UUID, *, grant_id: UUID
    ) -> RunSnapshot: ...

    def list_events(
        self,
        workspace_id: UUID,
        run_id: UUID,
        after_sequence: int,
        limit: int = 100,
        *,
        grant_id: UUID,
    ) -> EventPage: ...

    def send_input(self, request: RunInput) -> InputReceipt: ...

    def get_input(
        self,
        workspace_id: UUID,
        run_id: UUID,
        operation_id: UUID,
        *,
        grant_id: UUID,
    ) -> InputReceipt: ...

    def interrupt(self, request: RunInterrupt) -> InterruptReceipt: ...

    def get_interrupt(
        self,
        workspace_id: UUID,
        run_id: UUID,
        operation_id: UUID,
        *,
        grant_id: UUID,
    ) -> InterruptReceipt: ...

    def stop(self, request: RunStop) -> StopReceipt: ...

    def get_stop(
        self,
        workspace_id: UUID,
        run_id: UUID,
        operation_id: UUID,
        *,
        grant_id: UUID,
    ) -> StopReceipt: ...

    def recover(self, request: RunRecover) -> RecoveryReceipt: ...


class AgentObservations(Protocol):
    def list_outputs(
        self, workspace_id: UUID, run_id: UUID, *, grant_id: UUID
    ) -> tuple[OutputRef, ...]: ...

    def read_trace(
        self,
        workspace_id: UUID,
        run_id: UUID,
        after_sequence: int,
        limit: int = 100,
        *,
        grant_id: UUID,
    ) -> EventPage: ...
