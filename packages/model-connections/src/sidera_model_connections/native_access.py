import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from unicodedata import category
from uuid import UUID

from pydantic import SecretStr

from sidera_model_connections.contracts import CredentialMetadata, CredentialRef
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.ports import ConnectionCatalog, SecretStore

_ENVIRONMENT_DESTINATION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SECRET_DESTINATION = re.compile(
    r"(?:secret|password|token|api[_-]?key|credential|authorization|private[_-]?key)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NativeCredentialAccessRequest:
    workspace_id: UUID
    credential_ref: CredentialRef
    scope_id: UUID
    destination: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.workspace_id, UUID)
            or not isinstance(self.credential_ref, CredentialRef)
            or self.credential_ref.workspace_id != self.workspace_id
            or not isinstance(self.scope_id, UUID)
            or not isinstance(self.destination, str)
            or not self.destination.strip()
            or len(self.destination) > 256
            or _ENVIRONMENT_DESTINATION.fullmatch(self.destination) is None
            or _SECRET_DESTINATION.search(self.destination) is None
            or any(category(character) == "Cc" for character in self.destination)
        ):
            raise ConfigurationError("invalid_configuration")


@dataclass(frozen=True, slots=True)
class NativeCredentialAccessPermission:
    request: NativeCredentialAccessRequest

    def __post_init__(self) -> None:
        if not isinstance(self.request, NativeCredentialAccessRequest):
            raise ConfigurationError("invalid_configuration")


class NativeCredentialPermissionReader(Protocol):
    def authorize(
        self, request: NativeCredentialAccessRequest
    ) -> NativeCredentialAccessPermission: ...


class DenyAllNativeCredentialPermissionReader:
    def authorize(
        self, request: NativeCredentialAccessRequest
    ) -> NativeCredentialAccessPermission:
        raise ConfigurationError("runtime_binding_denied")


class NativeCredentialReader:
    def __init__(
        self,
        catalog: ConnectionCatalog,
        permission_reader: NativeCredentialPermissionReader,
        secret_store: SecretStore,
        env_sources: Mapping[str, SecretStr],
    ) -> None:
        self._catalog = catalog
        self._permission_reader = permission_reader
        self._secret_store = secret_store
        self._env_sources = env_sources

    def read(self, request: NativeCredentialAccessRequest) -> SecretStr:
        if not isinstance(request, NativeCredentialAccessRequest):
            raise ConfigurationError("invalid_configuration")

        try:
            permission = self._permission_reader.authorize(request)
        except Exception:  # noqa: BLE001
            raise ConfigurationError("runtime_binding_denied") from None
        if (
            not isinstance(permission, NativeCredentialAccessPermission)
            or permission.request != request
        ):
            raise ConfigurationError("runtime_binding_denied")

        try:
            with self._catalog.credential_read_lease(
                request.credential_ref
            ) as credential:
                credential = self._validate_credential(credential, request)
                if credential.status == "revoked":
                    raise ConfigurationError("credential_revoked")
                if credential.status != "active":
                    raise ConfigurationError("credential_unresolved")
                return self._read_selected_source(credential)
        except ConfigurationError as exc:
            raise ConfigurationError(exc.code) from None
        except Exception:  # noqa: BLE001
            raise ConfigurationError("credential_not_found") from None

    def _validate_credential(
        self,
        credential: object,
        request: NativeCredentialAccessRequest,
    ) -> CredentialMetadata:
        if (
            not isinstance(credential, CredentialMetadata)
            or credential.ref != request.credential_ref
            or credential.ref.workspace_id != request.workspace_id
        ):
            raise ConfigurationError("credential_not_found")
        return credential

    def _read_selected_source(self, credential: CredentialMetadata) -> SecretStr:
        if credential.source == "env":
            if credential.env_name is None:
                raise ConfigurationError("credential_unresolved")
            try:
                secret = self._env_sources[credential.env_name]
            except Exception:  # noqa: BLE001
                raise ConfigurationError("credential_unresolved") from None
        else:
            try:
                secret = self._secret_store.read(credential.ref)
            except ConfigurationError as exc:
                raise ConfigurationError(exc.code) from None
            except Exception:  # noqa: BLE001
                raise ConfigurationError("credential_store_unavailable") from None

        if not isinstance(secret, SecretStr) or not secret.get_secret_value():
            raise ConfigurationError("credential_unresolved")
        return secret
