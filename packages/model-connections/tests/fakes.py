from dataclasses import replace
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
    RuntimeAccessRequest,
)
from sidera_model_connections.errors import ConfigurationError


def fake_ref() -> CredentialRef:
    return CredentialRef(UUID(int=1), UUID(int=2))


class MemoryCatalog:
    def __init__(self) -> None:
        self.connections: dict[ConnectionRef, ModelConnectionRevision] = {}
        self.credentials: dict[CredentialRef, CredentialMetadata] = {}
        self.credential_writes: dict[tuple[UUID, UUID], CredentialWrite] = {}

    def get_connection(self, ref: ConnectionRef) -> ModelConnectionRevision:
        try:
            return self.connections[ref]
        except KeyError:
            raise ConfigurationError("configuration_not_found") from None

    def get_credential(self, ref: CredentialRef) -> CredentialMetadata:
        try:
            return self.credentials[ref]
        except KeyError:
            raise ConfigurationError("credential_not_found") from None

    def list_connections(self, workspace_id: UUID) -> tuple[ModelConnectionRevision, ...]:
        return tuple(
            connection
            for connection in self.connections.values()
            if connection.ref.workspace_id == workspace_id
        )

    def create_connection(self, connection: ModelConnectionRevision) -> None:
        if connection.ref in self.connections:
            raise ConfigurationError("configuration_version_conflict")
        self.connections[connection.ref] = connection

    def append_connection(
        self,
        connection: ModelConnectionRevision,
        *,
        expected_revision: int,
    ) -> None:
        current_revision = max(
            (
                ref.revision
                for ref in self.connections
                if ref.workspace_id == connection.ref.workspace_id
                and ref.connection_id == connection.ref.connection_id
            ),
            default=0,
        )
        if (
            current_revision != expected_revision
            or connection.ref.revision != expected_revision + 1
        ):
            raise ConfigurationError("configuration_version_conflict")
        self.connections[connection.ref] = connection

    def reserve_credential_write(self, write: CredentialWrite) -> CredentialWrite:
        key = (write.workspace_id, write.operation_id)
        if key in self.credential_writes:
            raise ConfigurationError("credential_operation_conflict")
        self.credential_writes[key] = write
        return write

    def get_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
    ) -> CredentialWrite:
        try:
            return self.credential_writes[(workspace_id, operation_id)]
        except KeyError:
            raise ConfigurationError("credential_write_unknown") from None

    def finish_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        *,
        status: CredentialWriteStatus,
    ) -> CredentialWrite:
        key = (workspace_id, operation_id)
        try:
            updated = replace(self.credential_writes[key], status=status)
        except KeyError:
            raise ConfigurationError("credential_write_unknown") from None
        self.credential_writes[key] = updated
        return updated

    def create_env_credential(self, metadata: CredentialMetadata) -> None:
        if metadata.ref in self.credentials:
            raise ConfigurationError("credential_already_exists")
        self.credentials[metadata.ref] = metadata

    def revoke_credential(
        self,
        ref: CredentialRef,
        *,
        expected_version: int,
    ) -> CredentialMetadata:
        try:
            current = self.credentials[ref]
        except KeyError:
            raise ConfigurationError("credential_not_found") from None
        if current.version != expected_version:
            raise ConfigurationError("configuration_version_conflict")
        updated = replace(current, status="revoked", version=current.version + 1)
        self.credentials[ref] = updated
        return updated


class FakeSecretStore:
    def __init__(self) -> None:
        self._values: dict[CredentialRef, SecretStr] = {}
        self.calls: list[tuple[str, CredentialRef]] = []

    def put_new(self, ref: CredentialRef, value: SecretStr) -> None:
        self.calls.append(("put_new", ref))
        if ref in self._values:
            raise ConfigurationError("credential_already_exists")
        self._values[ref] = value

    def read(self, ref: CredentialRef) -> SecretStr:
        self.calls.append(("read", ref))
        try:
            return self._values[ref]
        except KeyError:
            raise ConfigurationError("credential_unresolved") from None

    def delete_owned(self, ref: CredentialRef) -> None:
        self.calls.append(("delete_owned", ref))
        self._values.pop(ref, None)


class FakeBindingPermissionReader:
    def __init__(
        self,
        permissions: dict[RuntimeAccessRequest, BindingPermission] | None = None,
    ) -> None:
        self.permissions = dict(permissions or {})
        self.calls: list[RuntimeAccessRequest] = []

    def authorize(self, request: RuntimeAccessRequest) -> BindingPermission:
        self.calls.append(request)
        permission = self.permissions.get(request)
        if permission is None or permission.request != request:
            raise ConfigurationError("runtime_binding_denied")
        return permission
