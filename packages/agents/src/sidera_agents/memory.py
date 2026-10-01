from __future__ import annotations

from dataclasses import dataclass
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
)
from .errors import AgentError


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
