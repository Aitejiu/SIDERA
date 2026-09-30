import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import SecretStr

from sidera_model_connections.errors import (
    CONFIGURATION_ERROR_CODES,
    ConfigurationError,
)

Role = Literal["main_agent", "worker", "planner"]
CredentialSource = Literal["macos_keychain", "env"]
CredentialStatus = Literal["pending", "active", "revoked", "failed"]
CredentialWriteStatus = Literal["pending", "completed", "failed", "unknown"]
ProtocolName = Literal["openai_compatible"]
NetworkScope = Literal["public", "loopback"]

_ROLES = frozenset(Role.__args__)
_CREDENTIAL_SOURCES = frozenset(CredentialSource.__args__)
_CREDENTIAL_STATUSES = frozenset(CredentialStatus.__args__)
_CREDENTIAL_WRITE_STATUSES = frozenset(CredentialWriteStatus.__args__)
_PROTOCOLS = frozenset(ProtocolName.__args__)
_NETWORK_SCOPES = frozenset(NetworkScope.__args__)
_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")


def _invalid() -> None:
    raise ConfigurationError("invalid_configuration")


def _require_uuid(value: object) -> None:
    if not isinstance(value, UUID):
        _invalid()


def _require_positive_int(value: object) -> None:
    if type(value) is not int or value < 1:
        _invalid()


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _require_text(value: object, *, maximum: int | None = None) -> None:
    if not isinstance(value, str) or not value or _has_control(value):
        _invalid()
    if maximum is not None and len(value) > maximum:
        _invalid()


def _require_utc(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _invalid()
    offset = value.utcoffset()
    if offset is None or offset != timedelta(0):
        _invalid()


def _require_role_set(value: object) -> None:
    if not isinstance(value, frozenset) or not value or not value <= _ROLES:
        _invalid()


@dataclass(frozen=True, slots=True)
class ConnectionRef:
    workspace_id: UUID
    connection_id: UUID
    revision: int

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id)
        _require_uuid(self.connection_id)
        _require_positive_int(self.revision)


@dataclass(frozen=True, slots=True)
class CredentialRef:
    workspace_id: UUID
    credential_id: UUID

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id)
        _require_uuid(self.credential_id)


@dataclass(frozen=True, slots=True)
class ModelConnectionRevision:
    ref: ConnectionRef
    name: str
    provider_id: str
    protocol: ProtocolName
    base_url: str
    network_scope: NetworkScope
    allowed_roles: frozenset[Role]
    credential_ref: CredentialRef
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ConnectionRef):
            _invalid()
        if not isinstance(self.credential_ref, CredentialRef):
            _invalid()
        if self.ref.workspace_id != self.credential_ref.workspace_id:
            _invalid()
        _require_text(self.name, maximum=100)
        if not isinstance(self.provider_id, str) or not _PROVIDER_ID.fullmatch(
            self.provider_id
        ):
            _invalid()
        if (
            not isinstance(self.protocol, str)
            or self.protocol not in _PROTOCOLS
            or not isinstance(self.network_scope, str)
            or self.network_scope not in _NETWORK_SCOPES
        ):
            _invalid()
        _require_text(self.base_url)
        _require_role_set(self.allowed_roles)
        _require_utc(self.created_at)


@dataclass(frozen=True, slots=True)
class CredentialMetadata:
    ref: CredentialRef
    alias: str
    source: CredentialSource
    status: CredentialStatus
    env_name: str | None
    replaces_ref: CredentialRef | None
    created_at: datetime
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.ref, CredentialRef):
            _invalid()
        _require_text(self.alias, maximum=100)
        if not isinstance(self.source, str) or self.source not in _CREDENTIAL_SOURCES:
            _invalid()
        if not isinstance(self.status, str) or self.status not in _CREDENTIAL_STATUSES:
            _invalid()
        if self.source == "env":
            if not isinstance(self.env_name, str) or not _ENV_NAME.fullmatch(
                self.env_name
            ):
                _invalid()
        elif self.env_name is not None:
            _invalid()
        if self.replaces_ref is not None:
            if not isinstance(self.replaces_ref, CredentialRef):
                _invalid()
            if self.replaces_ref.workspace_id != self.ref.workspace_id:
                _invalid()
            if self.replaces_ref.credential_id == self.ref.credential_id:
                _invalid()
        _require_utc(self.created_at)
        _require_positive_int(self.version)


@dataclass(frozen=True, slots=True)
class RuntimeAccessRequest:
    workspace_id: UUID
    role: Role
    binding_id: UUID

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id)
        _require_uuid(self.binding_id)
        if not isinstance(self.role, str) or self.role not in _ROLES:
            _invalid()


@dataclass(frozen=True, slots=True)
class BindingPermission:
    request: RuntimeAccessRequest
    connection_ref: ConnectionRef
    allowed_protocols: frozenset[ProtocolName]
    allowed_origins: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.request, RuntimeAccessRequest):
            _invalid()
        if not isinstance(self.connection_ref, ConnectionRef):
            _invalid()
        if self.connection_ref.workspace_id != self.request.workspace_id:
            _invalid()
        if not isinstance(self.allowed_protocols, frozenset):
            _invalid()
        if not self.allowed_protocols <= _PROTOCOLS:
            _invalid()
        if not isinstance(self.allowed_origins, frozenset):
            _invalid()
        if any(not isinstance(origin, str) or not origin for origin in self.allowed_origins):
            _invalid()


@dataclass(frozen=True, slots=True)
class CredentialWrite:
    workspace_id: UUID
    operation_id: UUID
    credential: CredentialMetadata
    status: CredentialWriteStatus
    failure_code: str | None

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id)
        _require_uuid(self.operation_id)
        if not isinstance(self.credential, CredentialMetadata):
            _invalid()
        if self.workspace_id != self.credential.ref.workspace_id:
            _invalid()
        if (
            not isinstance(self.status, str)
            or self.status not in _CREDENTIAL_WRITE_STATUSES
        ):
            _invalid()
        if self.failure_code is not None and (
            not isinstance(self.failure_code, str)
            or self.failure_code not in CONFIGURATION_ERROR_CODES
        ):
            _invalid()


@dataclass(frozen=True, slots=True)
class ResolvedConnection:
    connection: ModelConnectionRevision
    credential: CredentialMetadata
    secret: SecretStr = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.connection, ModelConnectionRevision):
            _invalid()
        if not isinstance(self.credential, CredentialMetadata):
            _invalid()
        if self.connection.credential_ref != self.credential.ref:
            _invalid()
        if not isinstance(self.secret, SecretStr):
            _invalid()


@dataclass(frozen=True, slots=True)
class ConnectionSummary:
    workspace_id: UUID
    connection_id: UUID
    revision: int
    name: str
    provider_id: str
    protocol: ProtocolName
    base_url: str
    network_scope: NetworkScope
    allowed_roles: frozenset[Role]
    credential_id: UUID
    availability: str

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id)
        _require_uuid(self.connection_id)
        _require_positive_int(self.revision)
        _require_text(self.name, maximum=100)
        if not isinstance(self.provider_id, str) or not _PROVIDER_ID.fullmatch(
            self.provider_id
        ):
            _invalid()
        if (
            not isinstance(self.protocol, str)
            or self.protocol not in _PROTOCOLS
            or not isinstance(self.network_scope, str)
            or self.network_scope not in _NETWORK_SCOPES
        ):
            _invalid()
        _require_text(self.base_url)
        _require_role_set(self.allowed_roles)
        _require_uuid(self.credential_id)
        _require_text(self.availability)


@dataclass(frozen=True, slots=True)
class CredentialSummary:
    workspace_id: UUID
    credential_id: UUID
    alias: str
    source: CredentialSource
    status: CredentialStatus
    version: int
    replaces_id: UUID | None

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id)
        _require_uuid(self.credential_id)
        _require_text(self.alias, maximum=100)
        if not isinstance(self.source, str) or self.source not in _CREDENTIAL_SOURCES:
            _invalid()
        if not isinstance(self.status, str) or self.status not in _CREDENTIAL_STATUSES:
            _invalid()
        _require_positive_int(self.version)
        if self.replaces_id is not None:
            _require_uuid(self.replaces_id)
            if self.replaces_id == self.credential_id:
                _invalid()
