from collections.abc import Mapping

from pydantic import SecretStr

from sidera_model_connections.contracts import (
    BindingPermission,
    CredentialMetadata,
    ModelConnectionRevision,
    ResolvedConnection,
    RuntimeAccessRequest,
)
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.ports import (
    BindingPermissionReader,
    ConnectionCatalog,
    SecretStore,
)
from sidera_model_connections.validation import endpoint_origin, validate_endpoint


class ConnectionResolver:
    def __init__(
        self,
        catalog: ConnectionCatalog,
        permission_reader: BindingPermissionReader,
        secret_store: SecretStore,
        env_sources: Mapping[str, SecretStr],
    ) -> None:
        self._catalog = catalog
        self._permission_reader = permission_reader
        self._secret_store = secret_store
        self._env_sources = env_sources

    def resolve(self, request: RuntimeAccessRequest) -> ResolvedConnection:
        if not isinstance(request, RuntimeAccessRequest):
            raise ConfigurationError("invalid_configuration")
        try:
            permission = self._permission_reader.authorize(request)
        except Exception:  # noqa: BLE001
            raise ConfigurationError("runtime_binding_denied") from None
        if (
            not isinstance(permission, BindingPermission)
            or permission.request != request
            or permission.connection_ref.workspace_id != request.workspace_id
        ):
            raise ConfigurationError("runtime_binding_denied")

        try:
            connection = self._catalog.get_connection(permission.connection_ref)
        except ConfigurationError as exc:
            raise ConfigurationError(exc.code) from None
        except Exception:  # noqa: BLE001
            raise ConfigurationError("configuration_not_found") from None
        if not isinstance(connection, ModelConnectionRevision):
            raise ConfigurationError("configuration_not_found")
        if connection.ref != permission.connection_ref:
            raise ConfigurationError("configuration_not_found")
        if connection.ref.workspace_id != request.workspace_id:
            raise ConfigurationError("runtime_binding_denied")
        if request.role not in connection.allowed_roles:
            raise ConfigurationError("connection_role_denied")

        try:
            canonical_url = validate_endpoint(
                connection.base_url,
                connection.network_scope,
            )
            origin = endpoint_origin(canonical_url)
        except ConfigurationError:
            raise ConfigurationError("connection_target_denied") from None
        if (
            connection.protocol not in permission.allowed_protocols
            or origin not in permission.allowed_origins
        ):
            raise ConfigurationError("connection_target_denied")

        try:
            credential = self._catalog.get_credential(connection.credential_ref)
        except ConfigurationError as exc:
            raise ConfigurationError(exc.code) from None
        except Exception:  # noqa: BLE001
            raise ConfigurationError("credential_not_found") from None
        if (
            not isinstance(credential, CredentialMetadata)
            or credential.ref != connection.credential_ref
            or credential.ref.workspace_id != request.workspace_id
        ):
            raise ConfigurationError("credential_not_found")
        if credential.status == "revoked":
            raise ConfigurationError("credential_revoked")
        if credential.status != "active":
            raise ConfigurationError("credential_unresolved")

        secret = self._read_selected_source(credential)
        return ResolvedConnection(connection, credential, secret)

    def _read_selected_source(self, credential: CredentialMetadata) -> SecretStr:
        if credential.source == "env":
            if credential.env_name is None:
                raise ConfigurationError("credential_unresolved")
            try:
                secret = self._env_sources[credential.env_name]
            except KeyError:
                raise ConfigurationError("credential_unresolved") from None
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
