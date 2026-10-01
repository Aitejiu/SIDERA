from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from uuid import UUID

from .admission import OperationRecord
from .contracts import (
    CapabilityResourceDraft,
    NativeHandle,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    ProfileStatus,
    ResourceRef,
    ResourceRevision,
    ResourceStatus,
    RunSnapshot,
    RunState,
    RuntimeProbe,
    RuntimeRef,
    StartReceipt,
    StartStatus,
)
from .errors import AgentError
from .ports import RuntimeEntry, RuntimeInstallation, RuntimeObservation, RuntimeRecord


@dataclass
class _Head:
    revision: int
    enabled: bool = True
    status_version: int = 1


@dataclass
class _StoredOperation:
    profile_ref: ProfileRef
    profile_config_hash: str
    fingerprint: str
    receipt: StartReceipt


@dataclass
class _RunReservation:
    workspace_id: UUID
    profile_id: UUID
    installation_key: str
    active: bool = True
    session: bool = True


@dataclass
class _StoredRun:
    snapshot: RunSnapshot
    operation_id: UUID
    native_handle: NativeHandle | None = None
    terminal_state: RunState | None = None
    terminal_reason_code: str | None = None


def _profile_copy(value: ProfileRevision) -> ProfileRevision:
    return ProfileRevision.model_validate(
        value.model_dump(mode="python", warnings=False)
    )


def _resource_copy(value: ResourceRevision) -> ResourceRevision:
    return ResourceRevision.model_validate(
        value.model_dump(mode="python", warnings=False)
    )


def _runtime_entry_copy(value: RuntimeEntry) -> RuntimeEntry:
    observation = value.record.observation
    record = replace(
        value.record,
        observation=RuntimeObservation(
            probe=RuntimeProbe.model_validate(
                observation.probe.model_dump(mode="python", warnings=False)
            ),
            installed=observation.installed,
            file_identity=observation.file_identity,
            auth_state=observation.auth_state,
        ),
    )
    return RuntimeEntry(record=record, installation=value.installation)


def _run_copy(value: RunSnapshot) -> RunSnapshot:
    return RunSnapshot.model_validate(value.model_dump(mode="python", warnings=False))


def _receipt_copy(value: StartReceipt) -> StartReceipt:
    return StartReceipt.model_validate(value.model_dump(mode="python", warnings=False))


def _not_found(kind: str) -> AgentError:
    return AgentError("not_found", f"{kind.capitalize()} not found.")


def _revision_conflict(message: str) -> AgentError:
    return AgentError("revision_conflict", message)


class MemoryAgentStore:
    """Thread-safe ephemeral storage for tests and explicit local use."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._profile_heads: dict[tuple[UUID, UUID], _Head] = {}
        self._profile_revisions: dict[tuple[UUID, UUID, int], ProfileRevision] = {}
        self._resource_heads: dict[tuple[UUID, UUID], _Head] = {}
        self._resource_revisions: dict[tuple[UUID, UUID, int], ResourceRevision] = {}
        self._runtime_installations: dict[str, RuntimeInstallation] = {}
        self._runtime_records: dict[tuple[UUID, UUID], RuntimeRecord] = {}
        self._runtime_index: dict[tuple[UUID, str, str], UUID] = {}
        self._execution_owner: UUID | None = None
        self._run_operations: dict[tuple[UUID, UUID], _StoredOperation] = {}
        self._runs: dict[UUID, _StoredRun] = {}
        self._run_reservations: dict[UUID, _RunReservation] = {}

    def find_runtime(
        self, workspace_id: UUID, harness_id: str, executable: str
    ) -> RuntimeEntry | None:
        with self._lock:
            runtime_id = self._runtime_index.get((workspace_id, harness_id, executable))
            if runtime_id is None:
                return None
            return self._runtime_entry(
                self._runtime_records[(workspace_id, runtime_id)]
            )

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
    ) -> RuntimeEntry:
        with self._lock:
            unique_key = (workspace_id, harness_id, executable)
            existing_id = self._runtime_index.get(unique_key)
            if existing_id is not None:
                return self._runtime_entry(
                    self._runtime_records[(workspace_id, existing_id)]
                )

            installation = self._runtime_installations.get(installation_key)
            if installation is None:
                installation = RuntimeInstallation(
                    installation_key=installation_key,
                    harness_id=harness_id,
                    executable=executable,
                    max_active=max_active,
                    max_sessions=max_sessions,
                    enabled=True,
                    version=1,
                )
                self._runtime_installations[installation_key] = installation
            elif (installation.harness_id, installation.executable) != (
                harness_id,
                executable,
            ):
                raise AgentError("catalog_corrupt", "Runtime installation is invalid.")

            ref = RuntimeRef(workspace_id=workspace_id, runtime_id=runtime_id)
            record = RuntimeRecord(
                ref=ref,
                installation_key=installation_key,
                harness_id=harness_id,
                executable=executable,
                observation=observation,
            )
            self._runtime_records[(workspace_id, runtime_id)] = record
            self._runtime_index[unique_key] = runtime_id
            return self._runtime_entry(record)

    def get_runtime_entry(self, ref: RuntimeRef) -> RuntimeEntry:
        with self._lock:
            record = self._runtime_records.get((ref.workspace_id, ref.runtime_id))
            if record is None:
                raise _not_found("runtime")
            return self._runtime_entry(record)

    def list_runtime_entries(self, workspace_id: UUID) -> tuple[RuntimeEntry, ...]:
        with self._lock:
            records = sorted(
                (
                    record
                    for record in self._runtime_records.values()
                    if record.ref.workspace_id == workspace_id
                ),
                key=lambda record: (record.harness_id, record.executable),
            )
            return tuple(self._runtime_entry(record) for record in records)

    def update_runtime_observation(
        self, ref: RuntimeRef, observation: RuntimeObservation
    ) -> RuntimeEntry:
        with self._lock:
            key = (ref.workspace_id, ref.runtime_id)
            record = self._runtime_records.get(key)
            if record is None:
                raise _not_found("runtime")
            updated = replace(record, observation=observation)
            self._runtime_records[key] = updated
            return self._runtime_entry(updated)

    def set_runtime_configuration(
        self,
        ref: RuntimeRef,
        *,
        expected_version: int,
        max_active: int | None = None,
        max_sessions: int | None = None,
        enabled: bool | None = None,
    ) -> RuntimeEntry:
        with self._lock:
            record = self._runtime_records.get((ref.workspace_id, ref.runtime_id))
            if record is None:
                raise _not_found("runtime")
            installation = self._runtime_installations[record.installation_key]
            if installation.version != expected_version:
                raise _revision_conflict("Runtime configuration version is stale.")
            updated = replace(
                installation,
                max_active=(
                    installation.max_active if max_active is None else max_active
                ),
                max_sessions=(
                    installation.max_sessions if max_sessions is None else max_sessions
                ),
                enabled=installation.enabled if enabled is None else enabled,
                version=installation.version + 1,
            )
            self._runtime_installations[record.installation_key] = updated
            return self._runtime_entry(record)

    def _runtime_entry(self, record: RuntimeRecord) -> RuntimeEntry:
        installation = self._runtime_installations.get(record.installation_key)
        if installation is None:
            raise AgentError("catalog_corrupt", "Runtime installation is missing.")
        return _runtime_entry_copy(RuntimeEntry(record, installation))

    def create_profile(
        self,
        workspace_id: UUID,
        profile_id: UUID,
        draft: ProfileDraft,
        config_hash: str,
        created_at: datetime,
    ) -> ProfileRevision:
        with self._lock:
            key = (workspace_id, profile_id)
            if key in self._profile_heads:
                raise AgentError("operation_conflict", "Profile already exists.")
            self._require_enabled_resources(workspace_id, draft.resource_refs)
            ref = ProfileRef(
                workspace_id=workspace_id, profile_id=profile_id, revision=1
            )
            revision = ProfileRevision(
                ref=ref,
                draft=draft,
                config_hash=config_hash,
                created_at=created_at,
            )
            self._profile_heads[key] = _Head(revision=1)
            self._profile_revisions[(workspace_id, profile_id, 1)] = _profile_copy(
                revision
            )
            return _profile_copy(revision)

    def get_profile(self, ref: ProfileRef) -> ProfileRevision:
        with self._lock:
            value = self._profile_revisions.get(
                (ref.workspace_id, ref.profile_id, ref.revision)
            )
            if value is None:
                raise _not_found("profile")
            return _profile_copy(value)

    def get_profile_head(self, workspace_id: UUID, profile_id: UUID) -> ProfileRevision:
        with self._lock:
            head = self._profile_heads.get((workspace_id, profile_id))
            if head is None:
                raise _not_found("profile")
            return _profile_copy(
                self._profile_revisions[(workspace_id, profile_id, head.revision)]
            )

    def list_profiles(self, workspace_id: UUID) -> tuple[ProfileRevision, ...]:
        with self._lock:
            profile_ids = sorted(
                (
                    profile_id
                    for owner, profile_id in self._profile_heads
                    if owner == workspace_id
                ),
                key=str,
            )
            return tuple(
                _profile_copy(
                    self._profile_revisions[
                        (
                            workspace_id,
                            profile_id,
                            self._profile_heads[(workspace_id, profile_id)].revision,
                        )
                    ]
                )
                for profile_id in profile_ids
            )

    def append_profile_revision(
        self,
        expected_ref: ProfileRef,
        draft: ProfileDraft,
        config_hash: str,
        created_at: datetime,
    ) -> ProfileRevision:
        with self._lock:
            key = (expected_ref.workspace_id, expected_ref.profile_id)
            head = self._profile_heads.get(key)
            if head is None:
                raise _not_found("profile")
            if head.revision != expected_ref.revision:
                raise _revision_conflict("Profile revision is stale.")
            self._require_enabled_resources(
                expected_ref.workspace_id, draft.resource_refs
            )
            next_revision = head.revision + 1
            ref = ProfileRef(
                workspace_id=expected_ref.workspace_id,
                profile_id=expected_ref.profile_id,
                revision=next_revision,
            )
            revision = ProfileRevision(
                ref=ref,
                draft=draft,
                config_hash=config_hash,
                created_at=created_at,
            )
            self._profile_revisions[
                (ref.workspace_id, ref.profile_id, ref.revision)
            ] = _profile_copy(revision)
            head.revision = next_revision
            return _profile_copy(revision)

    def set_profile_enabled(
        self, workspace_id: UUID, profile_id: UUID, enabled: bool, expected_version: int
    ) -> ProfileStatus:
        with self._lock:
            head = self._profile_heads.get((workspace_id, profile_id))
            if head is None:
                raise _not_found("profile")
            if head.status_version != expected_version:
                raise _revision_conflict("Profile status version is stale.")
            head.enabled = enabled
            head.status_version += 1
            return ProfileStatus(
                profile_id=profile_id, enabled=head.enabled, version=head.status_version
            )

    def get_profile_status(self, workspace_id: UUID, profile_id: UUID) -> ProfileStatus:
        with self._lock:
            head = self._profile_heads.get((workspace_id, profile_id))
            if head is None:
                raise _not_found("profile")
            return ProfileStatus(
                profile_id=profile_id,
                enabled=head.enabled,
                version=head.status_version,
            )

    def create_resource(
        self,
        workspace_id: UUID,
        resource_id: UUID,
        draft: CapabilityResourceDraft,
        content_hash: str,
        created_at: datetime,
    ) -> ResourceRevision:
        with self._lock:
            key = (workspace_id, resource_id)
            if key in self._resource_heads:
                raise AgentError("operation_conflict", "Resource already exists.")
            ref = ResourceRef(
                workspace_id=workspace_id, resource_id=resource_id, revision=1
            )
            revision = ResourceRevision(
                ref=ref,
                draft=draft,
                content_hash=content_hash,
                created_at=created_at,
            )
            self._resource_heads[key] = _Head(revision=1)
            self._resource_revisions[(workspace_id, resource_id, 1)] = _resource_copy(
                revision
            )
            return _resource_copy(revision)

    def get_resource(self, ref: ResourceRef) -> ResourceRevision:
        with self._lock:
            value = self._resource_revisions.get(
                (ref.workspace_id, ref.resource_id, ref.revision)
            )
            if value is None:
                raise _not_found("resource")
            return _resource_copy(value)

    def get_resource_head(
        self, workspace_id: UUID, resource_id: UUID
    ) -> ResourceRevision:
        with self._lock:
            head = self._resource_heads.get((workspace_id, resource_id))
            if head is None:
                raise _not_found("resource")
            return _resource_copy(
                self._resource_revisions[(workspace_id, resource_id, head.revision)]
            )

    def list_resources(self, workspace_id: UUID) -> tuple[ResourceRevision, ...]:
        with self._lock:
            resource_ids = sorted(
                (
                    resource_id
                    for owner, resource_id in self._resource_heads
                    if owner == workspace_id
                ),
                key=str,
            )
            return tuple(
                _resource_copy(
                    self._resource_revisions[
                        (
                            workspace_id,
                            resource_id,
                            self._resource_heads[(workspace_id, resource_id)].revision,
                        )
                    ]
                )
                for resource_id in resource_ids
            )

    def append_resource_revision(
        self,
        expected_ref: ResourceRef,
        draft: CapabilityResourceDraft,
        content_hash: str,
        created_at: datetime,
    ) -> ResourceRevision:
        with self._lock:
            key = (expected_ref.workspace_id, expected_ref.resource_id)
            head = self._resource_heads.get(key)
            if head is None:
                raise _not_found("resource")
            if head.revision != expected_ref.revision:
                raise _revision_conflict("Resource revision is stale.")
            next_revision = head.revision + 1
            ref = ResourceRef(
                workspace_id=expected_ref.workspace_id,
                resource_id=expected_ref.resource_id,
                revision=next_revision,
            )
            revision = ResourceRevision(
                ref=ref,
                draft=draft,
                content_hash=content_hash,
                created_at=created_at,
            )
            self._resource_revisions[
                (ref.workspace_id, ref.resource_id, ref.revision)
            ] = _resource_copy(revision)
            head.revision = next_revision
            return _resource_copy(revision)

    def get_resource_status(
        self, workspace_id: UUID, resource_id: UUID
    ) -> ResourceStatus:
        with self._lock:
            head = self._resource_heads.get((workspace_id, resource_id))
            if head is None:
                raise _not_found("resource")
            return ResourceStatus(
                resource_id=resource_id,
                enabled=head.enabled,
                version=head.status_version,
            )

    def set_resource_enabled(
        self,
        workspace_id: UUID,
        resource_id: UUID,
        enabled: bool,
        expected_version: int,
    ) -> ResourceStatus:
        with self._lock:
            head = self._resource_heads.get((workspace_id, resource_id))
            if head is None:
                raise _not_found("resource")
            if head.status_version != expected_version:
                raise _revision_conflict("Resource status version is stale.")
            head.enabled = enabled
            head.status_version += 1
            return ResourceStatus(
                resource_id=resource_id,
                enabled=head.enabled,
                version=head.status_version,
            )

    def _require_enabled_resources(
        self, workspace_id: UUID, refs: tuple[ResourceRef, ...]
    ) -> None:
        for ref in refs:
            if ref.workspace_id != workspace_id:
                raise AgentError(
                    "configuration_invalid",
                    "Resource reference belongs to another workspace.",
                )
            key = (ref.workspace_id, ref.resource_id)
            revision = self._resource_revisions.get((*key, ref.revision))
            if revision is None:
                raise _not_found("resource")
            head = self._resource_heads[key]
            if not head.enabled:
                raise AgentError(
                    "configuration_invalid", "Disabled resources cannot be newly bound."
                )

    def acquire_execution_owner(self, owner_id: UUID) -> bool:
        with self._lock:
            if self._execution_owner not in (None, owner_id):
                return False
            self._execution_owner = owner_id
            return True

    def execution_owner_is_held(self, owner_id: UUID) -> bool:
        with self._lock:
            return self._execution_owner == owner_id

    def release_execution_owner(self, owner_id: UUID) -> None:
        with self._lock:
            if self._execution_owner == owner_id:
                self._execution_owner = None

    def admit_start(
        self,
        *,
        owner_id: UUID,
        profile_ref: ProfileRef,
        runtime_ref: RuntimeRef,
        installation_key: str,
        operation_id: UUID,
        profile_config_hash: str,
        fingerprint: str,
        run_id: UUID,
        snapshot: RunSnapshot,
    ) -> tuple[StartReceipt, bool]:
        """Atomically deduplicate a start and reserve memory-backed capacity."""
        with self._lock:
            if self._execution_owner != owner_id:
                raise AgentError(
                    "execution_owner_unavailable",
                    "The execution owner lock is not held.",
                )
            operation_key = (profile_ref.workspace_id, operation_id)
            operation = self._run_operations.get(operation_key)
            if operation is not None:
                if (
                    operation.profile_ref != profile_ref
                    or operation.profile_config_hash != profile_config_hash
                    or operation.fingerprint != fingerprint
                ):
                    raise AgentError(
                        "operation_conflict",
                        "Operation ID was used with a different request.",
                    )
                if operation.receipt.status is not StartStatus.BUSY:
                    return _receipt_copy(operation.receipt), False

            revision = self._profile_revisions.get(
                (profile_ref.workspace_id, profile_ref.profile_id, profile_ref.revision)
            )
            profile_head = self._profile_heads.get(
                (profile_ref.workspace_id, profile_ref.profile_id)
            )
            current_revision = (
                None
                if profile_head is None
                else self._profile_revisions.get(
                    (
                        profile_ref.workspace_id,
                        profile_ref.profile_id,
                        profile_head.revision,
                    )
                )
            )
            record = self._runtime_records.get(
                (runtime_ref.workspace_id, runtime_ref.runtime_id)
            )
            installation = self._runtime_installations.get(installation_key)
            if (
                revision is None
                or profile_head is None
                or current_revision is None
                or record is None
                or record.installation_key != installation_key
                or installation is None
            ):
                raise AgentError(
                    "configuration_invalid", "Run configuration is unavailable."
                )
            if not profile_head.enabled or not installation.enabled:
                raise AgentError(
                    "configuration_invalid", "Run configuration is disabled."
                )

            profile_active = sum(
                reservation.active
                for reservation in self._run_reservations.values()
                if reservation.workspace_id == profile_ref.workspace_id
                and reservation.profile_id == profile_ref.profile_id
            )
            busy = (
                profile_active >= current_revision.draft.max_active
                or installation.active_count >= installation.max_active
                or installation.session_count >= installation.max_sessions
            )
            if busy:
                receipt = StartReceipt(
                    operation_id=operation_id,
                    status=StartStatus.BUSY,
                    run_id=None,
                    reason_code="resource_busy",
                )
                self._run_operations[operation_key] = _StoredOperation(
                    profile_ref=profile_ref,
                    profile_config_hash=profile_config_hash,
                    fingerprint=fingerprint,
                    receipt=receipt,
                )
                return _receipt_copy(receipt), False

            if snapshot.run_id in self._runs:
                raise AgentError("operation_conflict", "Run ID is already in use.")
            receipt = StartReceipt(
                operation_id=operation_id,
                status=StartStatus.ACCEPTED,
                run_id=run_id,
                reason_code=None,
            )
            self._run_operations[operation_key] = _StoredOperation(
                profile_ref=profile_ref,
                profile_config_hash=profile_config_hash,
                fingerprint=fingerprint,
                receipt=receipt,
            )
            self._runs[run_id] = _StoredRun(
                snapshot=_run_copy(snapshot), operation_id=operation_id
            )
            self._run_reservations[run_id] = _RunReservation(
                workspace_id=profile_ref.workspace_id,
                profile_id=profile_ref.profile_id,
                installation_key=installation_key,
            )
            self._runtime_installations[installation_key] = replace(
                installation,
                active_count=installation.active_count + 1,
                session_count=installation.session_count + 1,
            )
            return _receipt_copy(receipt), True

    def get_start_receipt(
        self, workspace_id: UUID, operation_id: UUID
    ) -> StartReceipt | None:
        with self._lock:
            operation = self._run_operations.get((workspace_id, operation_id))
            return None if operation is None else _receipt_copy(operation.receipt)

    def get_start_operation(
        self, workspace_id: UUID, operation_id: UUID
    ) -> OperationRecord | None:
        with self._lock:
            operation = self._run_operations.get((workspace_id, operation_id))
            if operation is None:
                return None
            return OperationRecord(
                profile_ref=operation.profile_ref,
                profile_config_hash=operation.profile_config_hash,
                fingerprint=operation.fingerprint,
                receipt=_receipt_copy(operation.receipt),
            )

    def get_run_snapshot(self, workspace_id: UUID, run_id: UUID) -> RunSnapshot:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.snapshot.workspace_id != workspace_id:
                raise _not_found("run")
            return _run_copy(run.snapshot)

    def list_starting_runs(self) -> tuple[RunSnapshot, ...]:
        with self._lock:
            return tuple(
                _run_copy(run.snapshot)
                for run in self._runs.values()
                if run.snapshot.state.value == "starting"
            )

    def list_unresolved_runs(
        self,
    ) -> tuple[tuple[RunSnapshot, NativeHandle, RunState | None, str | None], ...]:
        with self._lock:
            return tuple(
                (
                    _run_copy(run.snapshot),
                    run.native_handle,
                    run.terminal_state,
                    run.terminal_reason_code,
                )
                for run in self._runs.values()
                if run.snapshot.state
                in (RunState.RUNNING, RunState.UNKNOWN, RunState.CLOSING)
                and run.native_handle is not None
            )

    def transition_run(
        self,
        run_id: UUID,
        snapshot: RunSnapshot,
        *,
        owner_id: UUID,
        native_handle: NativeHandle | None = None,
        replace_native_handle: bool = False,
        receipt_status: StartStatus | None = None,
        active_reserved: bool | None = None,
        session_reserved: bool | None = None,
        terminal_state: RunState | None = None,
        terminal_reason_code: str | None = None,
    ) -> None:
        """Persist a run outcome and release only evidence-backed reservations."""
        with self._lock:
            if self._execution_owner != owner_id:
                raise AgentError(
                    "execution_owner_unavailable",
                    "The execution owner lock is not held.",
                )
            run = self._runs.get(run_id)
            reservation = self._run_reservations.get(run_id)
            if run is None or reservation is None or snapshot.run_id != run_id:
                raise _not_found("run")
            installation = self._runtime_installations.get(reservation.installation_key)
            if installation is None:
                raise AgentError("catalog_corrupt", "Runtime installation is missing.")

            active_delta = (
                int(active_reserved) - int(reservation.active)
                if active_reserved is not None
                else 0
            )
            session_delta = (
                int(session_reserved) - int(reservation.session)
                if session_reserved is not None
                else 0
            )
            if (
                installation.active_count + active_delta < 0
                or installation.session_count + session_delta < 0
            ):
                raise AgentError("catalog_corrupt", "Run reservation count is invalid.")
            self._runtime_installations[reservation.installation_key] = replace(
                installation,
                active_count=installation.active_count + active_delta,
                session_count=installation.session_count + session_delta,
            )
            if active_reserved is not None:
                reservation.active = active_reserved
            if session_reserved is not None:
                reservation.session = session_reserved
            run.snapshot = _run_copy(snapshot)
            if terminal_state is not None:
                run.terminal_state = terminal_state
                run.terminal_reason_code = terminal_reason_code
            if replace_native_handle:
                run.native_handle = native_handle
            if receipt_status is not None:
                operation = self._run_operations[
                    (snapshot.workspace_id, run.operation_id)
                ]
                operation.receipt = StartReceipt(
                    operation_id=run.operation_id,
                    status=receipt_status,
                    run_id=run_id,
                    reason_code=snapshot.reason_code,
                )
