from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr

from sidera_model_connections.contracts import (
    BindingPermission,
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    CredentialWrite,
    CredentialWriteStatus,
    ModelConnectionRevision,
    ResolvedConnection,
    RuntimeAccessRequest,
)
from sidera_model_connections.errors import ConfigurationError


class BindingPermissionReader(Protocol):
    def authorize(self, request: RuntimeAccessRequest) -> BindingPermission: ...


class DenyAllBindingPermissionReader:
    def authorize(self, request: RuntimeAccessRequest) -> BindingPermission:
        raise ConfigurationError("runtime_binding_denied")


class SecretStore(Protocol):
    def put_new(self, ref: CredentialRef, value: SecretStr) -> None: ...

    def read(self, ref: CredentialRef) -> SecretStr: ...

    def delete_owned(self, ref: CredentialRef) -> None: ...


class ConnectionCatalog(Protocol):
    def get_connection(self, ref: ConnectionRef) -> ModelConnectionRevision: ...

    def get_credential(self, ref: CredentialRef) -> CredentialMetadata: ...

    def credential_read_lease(
        self, ref: CredentialRef
    ) -> AbstractContextManager[CredentialMetadata]: ...

    def list_connections(self, workspace_id: UUID) -> tuple[ModelConnectionRevision, ...]: ...

    def create_connection(self, connection: ModelConnectionRevision) -> None: ...

    def append_connection(
        self,
        connection: ModelConnectionRevision,
        *,
        expected_revision: int,
    ) -> None: ...

    def reserve_credential_write(self, write: CredentialWrite) -> CredentialWrite: ...

    def get_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
    ) -> CredentialWrite: ...

    def finish_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        *,
        status: CredentialWriteStatus,
    ) -> CredentialWrite: ...

    def create_env_credential(self, metadata: CredentialMetadata) -> None: ...

    def revoke_credential(
        self,
        ref: CredentialRef,
        *,
        expected_version: int,
    ) -> CredentialMetadata: ...


class ConnectionResolver(Protocol):
    def __init__(
        self,
        catalog: ConnectionCatalog,
        permission_reader: BindingPermissionReader,
        secret_store: SecretStore,
        env_sources: Mapping[str, SecretStr],
    ) -> None: ...

    def resolve(self, request: RuntimeAccessRequest) -> ResolvedConnection: ...
