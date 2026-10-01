from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from .contracts import (
    CapabilityResourceDraft,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    ProfileStatus,
    ResourceRef,
    ResourceRevision,
    ResourceStatus,
    SecretBinding,
)
from .errors import AgentError
from .ports import ExecutionAuthorizer


class AgentStore(Protocol):
    def create_profile(
        self,
        workspace_id: UUID,
        profile_id: UUID,
        draft: ProfileDraft,
        config_hash: str,
        created_at: datetime,
    ) -> ProfileRevision: ...

    def get_profile(self, ref: ProfileRef) -> ProfileRevision: ...

    def get_profile_head(
        self, workspace_id: UUID, profile_id: UUID
    ) -> ProfileRevision: ...

    def list_profiles(self, workspace_id: UUID) -> tuple[ProfileRevision, ...]: ...

    def append_profile_revision(
        self,
        expected_ref: ProfileRef,
        draft: ProfileDraft,
        config_hash: str,
        created_at: datetime,
    ) -> ProfileRevision: ...

    def set_profile_enabled(
        self, workspace_id: UUID, profile_id: UUID, enabled: bool, expected_version: int
    ) -> ProfileStatus: ...

    def create_resource(
        self,
        workspace_id: UUID,
        resource_id: UUID,
        draft: CapabilityResourceDraft,
        content_hash: str,
        created_at: datetime,
    ) -> ResourceRevision: ...

    def get_resource(self, ref: ResourceRef) -> ResourceRevision: ...

    def get_resource_head(
        self, workspace_id: UUID, resource_id: UUID
    ) -> ResourceRevision: ...

    def list_resources(self, workspace_id: UUID) -> tuple[ResourceRevision, ...]: ...

    def append_resource_revision(
        self,
        expected_ref: ResourceRef,
        draft: CapabilityResourceDraft,
        content_hash: str,
        created_at: datetime,
    ) -> ResourceRevision: ...

    def get_resource_status(
        self, workspace_id: UUID, resource_id: UUID
    ) -> ResourceStatus: ...

    def set_resource_enabled(
        self,
        workspace_id: UUID,
        resource_id: UUID,
        enabled: bool,
        expected_version: int,
    ) -> ResourceStatus: ...


@dataclass(frozen=True)
class _CatalogTarget:
    workspace_id: UUID
    entity: str
    entity_id: UUID


_SECRET_KEY = re.compile(
    r"(?:secret|password|token|api[_-]?key|credential|authorization|private[_-]?key)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bBearer\s+\S+)",
    re.IGNORECASE,
)
_RESERVED_ENVIRONMENT_KEYS = {
    "HOME",
    "PATH",
    "PWD",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "XDG_CONFIG_HOME",
    "OPENCODE_CONFIG",
    "CODEX_HOME",
}
_RESERVED_ENVIRONMENT_PREFIXES = ("SIDERA_", "OPENCODE_", "CODEX_")


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_profile_draft(draft: ProfileDraft) -> ProfileDraft:
    return ProfileDraft.model_validate(draft.model_dump(mode="python", warnings=False))


def _normalize_resource_draft(
    draft: CapabilityResourceDraft,
) -> CapabilityResourceDraft:
    return CapabilityResourceDraft.model_validate(
        draft.model_dump(mode="python", warnings=False)
    )


def _check_secret_free_values(
    value: object, *, field: str, environment: bool = False
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.search(key):
                raise AgentError(
                    "configuration_invalid",
                    "Secret values must be supplied through secret references.",
                    field,
                )
            if environment and (
                key.upper() in _RESERVED_ENVIRONMENT_KEYS
                or key.upper().startswith(_RESERVED_ENVIRONMENT_PREFIXES)
            ):
                raise AgentError(
                    "configuration_invalid", "Environment key is reserved.", field
                )
            _check_secret_free_values(child, field=field)
    elif isinstance(value, list):
        for child in value:
            _check_secret_free_values(child, field=field)
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        raise AgentError(
            "configuration_invalid",
            "Secret values must be supplied through secret references.",
            field,
        )


class ProfileCatalog:
    def __init__(
        self,
        store: AgentStore,
        authorizer: ExecutionAuthorizer,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._authorizer = authorizer
        self._clock = clock

    def create_profile(
        self, workspace_id: UUID, draft: ProfileDraft, *, grant_id: UUID
    ) -> ProfileRevision:
        self._authorizer.require(grant_id, "profile.create", workspace_id)
        normalized = _normalize_profile_draft(draft)
        resource_hashes = self._validate_profile_draft(
            workspace_id, normalized, grant_id
        )
        return self._store.create_profile(
            workspace_id,
            uuid4(),
            normalized,
            self._profile_hash(normalized, resource_hashes),
            self._clock(),
        )

    def revise_profile(
        self, ref: ProfileRef, draft: ProfileDraft, *, grant_id: UUID
    ) -> ProfileRevision:
        self._authorizer.require(grant_id, "profile.revise", ref)
        current = self._store.get_profile_head(ref.workspace_id, ref.profile_id)
        if current.ref != ref:
            raise AgentError("revision_conflict", "Profile revision is stale.")
        normalized = _normalize_profile_draft(draft)
        resource_hashes = self._validate_profile_draft(
            ref.workspace_id, normalized, grant_id
        )
        return self._store.append_profile_revision(
            ref,
            normalized,
            self._profile_hash(normalized, resource_hashes),
            self._clock(),
        )

    def get_profile(self, ref: ProfileRef, *, grant_id: UUID) -> ProfileRevision:
        self._authorizer.require(grant_id, "profile.read", ref)
        return self._store.get_profile(ref)

    def list_profiles(
        self, workspace_id: UUID, *, grant_id: UUID
    ) -> tuple[ProfileRevision, ...]:
        self._authorizer.require(grant_id, "profile.list", workspace_id)
        return self._store.list_profiles(workspace_id)

    def create_resource(
        self,
        workspace_id: UUID,
        draft: CapabilityResourceDraft,
        *,
        grant_id: UUID,
    ) -> ResourceRevision:
        self._authorizer.require(grant_id, "resource.create", workspace_id)
        normalized = _normalize_resource_draft(draft)
        self._validate_resource_draft(workspace_id, normalized)
        return self._store.create_resource(
            workspace_id,
            uuid4(),
            normalized,
            _canonical_hash(normalized.model_dump(mode="json")),
            self._clock(),
        )

    def revise_resource(
        self, ref: ResourceRef, draft: CapabilityResourceDraft, *, grant_id: UUID
    ) -> ResourceRevision:
        self._authorizer.require(grant_id, "resource.revise", ref)
        current = self._store.get_resource_head(ref.workspace_id, ref.resource_id)
        if current.ref != ref:
            raise AgentError("revision_conflict", "Resource revision is stale.")
        normalized = _normalize_resource_draft(draft)
        self._validate_resource_draft(ref.workspace_id, normalized)
        return self._store.append_resource_revision(
            ref,
            normalized,
            _canonical_hash(normalized.model_dump(mode="json")),
            self._clock(),
        )

    def get_resource(self, ref: ResourceRef, *, grant_id: UUID) -> ResourceRevision:
        self._authorizer.require(grant_id, "resource.read", ref)
        return self._store.get_resource(ref)

    def list_resources(
        self, workspace_id: UUID, *, grant_id: UUID
    ) -> tuple[ResourceRevision, ...]:
        self._authorizer.require(grant_id, "resource.list", workspace_id)
        return self._store.list_resources(workspace_id)

    def set_profile_enabled(
        self,
        workspace_id: UUID,
        profile_id: UUID,
        enabled: bool,
        expected_version: int,
        *,
        grant_id: UUID,
    ) -> ProfileStatus:
        self._validate_status_input(enabled, expected_version)
        action = "profile.enable" if enabled else "profile.disable"
        self._authorizer.require(
            grant_id, action, _CatalogTarget(workspace_id, "profile", profile_id)
        )
        return self._store.set_profile_enabled(
            workspace_id, profile_id, enabled, expected_version
        )

    def set_resource_enabled(
        self,
        workspace_id: UUID,
        resource_id: UUID,
        enabled: bool,
        expected_version: int,
        *,
        grant_id: UUID,
    ) -> ResourceStatus:
        self._validate_status_input(enabled, expected_version)
        action = "resource.enable" if enabled else "resource.disable"
        self._authorizer.require(
            grant_id, action, _CatalogTarget(workspace_id, "resource", resource_id)
        )
        return self._store.set_resource_enabled(
            workspace_id, resource_id, enabled, expected_version
        )

    def _validate_profile_draft(
        self, workspace_id: UUID, draft: ProfileDraft, grant_id: UUID
    ) -> tuple[str, ...]:
        if draft.runtime_ref.workspace_id != workspace_id:
            raise AgentError(
                "configuration_invalid",
                "Runtime reference belongs to another workspace.",
                "runtime_ref",
            )
        if not draft.name.strip():
            raise AgentError(
                "configuration_invalid", "Profile name is required.", "name"
            )
        _check_secret_free_values(
            draft.environment, field="environment", environment=True
        )
        _check_secret_free_values(draft.native_settings, field="native_settings")
        self._validate_secret_refs(workspace_id, draft.secret_refs, "secret_refs")

        seen: set[tuple[UUID, UUID]] = set()
        resource_hashes: list[str] = []
        for ref in draft.resource_refs:
            if ref.workspace_id != workspace_id:
                raise AgentError(
                    "configuration_invalid",
                    "Resource reference belongs to another workspace.",
                    "resource_refs",
                )
            key = (ref.workspace_id, ref.resource_id)
            if key in seen:
                raise AgentError(
                    "configuration_invalid",
                    "Duplicate resource reference.",
                    "resource_refs",
                )
            seen.add(key)
            self._authorizer.require(grant_id, "resource.read", ref)
            resource = self._store.get_resource(ref)
            status = self._store.get_resource_status(ref.workspace_id, ref.resource_id)
            if not status.enabled:
                raise AgentError(
                    "configuration_invalid",
                    "Disabled resources cannot be newly bound.",
                    "resource_refs",
                )
            resource_hashes.append(resource.content_hash)
        return tuple(resource_hashes)

    @staticmethod
    def _validate_resource_draft(
        workspace_id: UUID, draft: CapabilityResourceDraft
    ) -> None:
        if not draft.name.strip():
            raise AgentError(
                "configuration_invalid", "Resource name is required.", "name"
            )
        _check_secret_free_values(draft.settings, field="settings")
        ProfileCatalog._validate_secret_refs(
            workspace_id, draft.secret_refs, "secret_refs"
        )

    @staticmethod
    def _validate_secret_refs(
        workspace_id: UUID, bindings: tuple[SecretBinding, ...], field: str
    ) -> None:
        destinations: set[str] = set()
        for binding in bindings:
            if binding.credential_ref.workspace_id != workspace_id:
                raise AgentError(
                    "configuration_invalid",
                    "Credential reference belongs to another workspace.",
                    field,
                )
            destination = binding.destination.strip()
            if (
                not destination
                or destination.casefold() in destinations
                or _SECRET_KEY.search(destination) is None
            ):
                raise AgentError(
                    "configuration_invalid",
                    "Secret binding destination is invalid.",
                    field,
                )
            destinations.add(destination.casefold())

    @staticmethod
    def _validate_status_input(enabled: bool, expected_version: int) -> None:
        if type(enabled) is not bool:
            raise AgentError(
                "configuration_invalid", "Enabled must be a boolean.", "enabled"
            )
        if type(expected_version) is not int or expected_version < 1:
            raise AgentError(
                "configuration_invalid",
                "Expected version must be a positive integer.",
                "expected_version",
            )

    @staticmethod
    def _profile_hash(draft: ProfileDraft, resource_hashes: tuple[str, ...]) -> str:
        return _canonical_hash(
            {
                "draft": draft.model_dump(mode="json"),
                "resource_hashes": resource_hashes,
            }
        )
