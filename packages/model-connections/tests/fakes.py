from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from threading import RLock
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
        self.events: list[tuple[str, CredentialRef, UUID | None]] = []
        self._credential_lock = RLock()

    def get_connection(self, ref: ConnectionRef) -> ModelConnectionRevision:
        try:
            return self.connections[ref]
        except KeyError:
            raise ConfigurationError("configuration_not_found") from None

    def get_credential(self, ref: CredentialRef) -> CredentialMetadata:
        with self._credential_lock:
            try:
                return self.credentials[ref]
            except KeyError:
                raise ConfigurationError("credential_not_found") from None

    @contextmanager
    def credential_read_lease(self, ref: CredentialRef) -> Iterator[CredentialMetadata]:
        with self._credential_lock:
            yield self.get_credential(ref)

    def list_connections(self, workspace_id: UUID) -> tuple[ModelConnectionRevision, ...]:
        heads: dict[UUID, ModelConnectionRevision] = {}
        for connection in self.connections.values():
            if connection.ref.workspace_id != workspace_id:
                continue
            current = heads.get(connection.ref.connection_id)
            if current is None or connection.ref.revision > current.ref.revision:
                heads[connection.ref.connection_id] = connection
        return tuple(heads.values())

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
        existing = self.credential_writes.get(key)
        if existing is not None:
            stored = existing.credential
            requested = write.credential
            if (
                stored.alias != requested.alias
                or stored.source != requested.source
                or stored.env_name != requested.env_name
                or stored.replaces_ref != requested.replaces_ref
            ):
                raise ConfigurationError("credential_operation_conflict")
            return self._hydrate_credential_write(existing)

        credential = write.credential
        if (
            write.status == "pending"
            and write.failure_code is None
            and credential.source == "macos_keychain"
            and credential.status == "pending"
        ):
            if credential.ref in self.credentials:
                raise ConfigurationError("credential_already_exists")
            self.credentials[credential.ref] = credential
        self.credential_writes[key] = write
        return write

    def get_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
    ) -> CredentialWrite:
        try:
            write = self.credential_writes[(workspace_id, operation_id)]
        except KeyError:
            raise ConfigurationError("credential_write_unknown") from None
        return self._hydrate_credential_write(write)

    def finish_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        *,
        status: CredentialWriteStatus,
    ) -> CredentialWrite:
        key = (workspace_id, operation_id)
        try:
            current = self._hydrate_credential_write(self.credential_writes[key])
        except KeyError:
            raise ConfigurationError("credential_write_unknown") from None

        if status not in {"completed", "failed", "unknown"}:
            raise ConfigurationError("invalid_configuration")
        if current.status == status:
            return current
        if current.status not in {"pending", "unknown"}:
            raise ConfigurationError("credential_operation_conflict")

        credential = current.credential
        if status in {"completed", "failed"}:
            stored = self.credentials.get(credential.ref, credential)
            if stored.status != "pending":
                raise ConfigurationError("credential_operation_conflict")
            credential = replace(
                stored,
                status="active" if status == "completed" else "failed",
                version=stored.version + 1,
            )
            self.credentials[credential.ref] = credential
        updated = replace(current, credential=credential, status=status)
        self.credential_writes[key] = updated
        event_type = {
            "completed": "rotated" if credential.replaces_ref else "created",
            "failed": "write_failed",
            "unknown": "write_unknown",
        }[status]
        self.events.append((event_type, credential.ref, operation_id))
        return updated

    def _hydrate_credential_write(self, write: CredentialWrite) -> CredentialWrite:
        credential = self.credentials.get(write.credential.ref)
        if credential is None or credential == write.credential:
            return write
        return replace(write, credential=credential)

    def create_env_credential(self, metadata: CredentialMetadata) -> None:
        if metadata.ref in self.credentials:
            raise ConfigurationError("credential_already_exists")
        self.credentials[metadata.ref] = metadata
        self.events.append(("created", metadata.ref, None))

    def revoke_credential(
        self,
        ref: CredentialRef,
        *,
        expected_version: int,
    ) -> CredentialMetadata:
        with self._credential_lock:
            try:
                current = self.credentials[ref]
            except KeyError:
                raise ConfigurationError("credential_not_found") from None
            if current.version != expected_version or current.status != "active":
                raise ConfigurationError("configuration_version_conflict")
            updated = replace(current, status="revoked", version=current.version + 1)
            self.credentials[ref] = updated
            self.events.append(("revoked", ref, None))
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
