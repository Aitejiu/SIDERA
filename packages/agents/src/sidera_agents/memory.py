from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from uuid import UUID

from .contracts import (
    CapabilityResourceDraft,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    ProfileStatus,
    ResourceRef,
    ResourceRevision,
    ResourceStatus,
    RuntimeProbe,
    RuntimeRef,
)
from .errors import AgentError
from .ports import RuntimeEntry, RuntimeInstallation, RuntimeObservation, RuntimeRecord


@dataclass
class _Head:
    revision: int
    enabled: bool = True
    status_version: int = 1


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
