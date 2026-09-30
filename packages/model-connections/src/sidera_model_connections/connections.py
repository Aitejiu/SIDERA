from dataclasses import replace

from sidera_model_connections.contracts import (
    CredentialMetadata,
    ModelConnectionRevision,
)
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.ports import ConnectionCatalog
from sidera_model_connections.validation import validate_endpoint


class ConnectionService:
    def __init__(self, catalog: ConnectionCatalog) -> None:
        self._catalog = catalog

    def create(self, connection: ModelConnectionRevision) -> ModelConnectionRevision:
        if not isinstance(connection, ModelConnectionRevision):
            raise ConfigurationError("invalid_configuration")
        if connection.ref.revision != 1:
            raise ConfigurationError("invalid_configuration")
        saved = self._validate(connection)
        self._catalog.create_connection(saved)
        return saved

    def revise(
        self,
        connection: ModelConnectionRevision,
        *,
        expected_revision: int,
    ) -> ModelConnectionRevision:
        if not isinstance(connection, ModelConnectionRevision):
            raise ConfigurationError("invalid_configuration")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ConfigurationError("invalid_configuration")
        if connection.ref.revision != expected_revision + 1:
            raise ConfigurationError("configuration_version_conflict")
        revised = self._validate(connection)
        self._catalog.append_connection(
            revised,
            expected_revision=expected_revision,
        )
        return revised

    def _validate(self, connection: ModelConnectionRevision) -> ModelConnectionRevision:
        base_url = validate_endpoint(connection.base_url, connection.network_scope)
        credential = self._catalog.get_credential(connection.credential_ref)
        if not isinstance(credential, CredentialMetadata):
            raise ConfigurationError("credential_not_found")
        if credential.ref != connection.credential_ref:
            raise ConfigurationError("credential_not_found")
        if credential.status == "revoked":
            raise ConfigurationError("credential_revoked")
        if credential.status != "active":
            raise ConfigurationError("credential_unresolved")
        if base_url == connection.base_url:
            return connection
        return replace(connection, base_url=base_url)
