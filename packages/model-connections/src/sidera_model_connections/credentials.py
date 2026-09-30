from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import SecretStr

from sidera_model_connections.contracts import (
    CredentialMetadata,
    CredentialRef,
    CredentialWrite,
)
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.ports import ConnectionCatalog, SecretStore


@dataclass(frozen=True, slots=True)
class CredentialRevocationResult:
    credential: CredentialMetadata
    cleanup_pending: bool

    def __post_init__(self) -> None:
        if not isinstance(self.credential, CredentialMetadata) or type(
            self.cleanup_pending
        ) is not bool:
            raise ConfigurationError("invalid_configuration")


def _same_operation_metadata(
    stored: CredentialMetadata,
    requested: CredentialMetadata,
) -> bool:
    return (
        stored.alias == requested.alias
        and stored.source == requested.source
        and stored.env_name == requested.env_name
        and stored.replaces_ref == requested.replaces_ref
    )


def _validate_managed_secret(value: object) -> SecretStr:
    if not isinstance(value, SecretStr):
        raise ConfigurationError("invalid_configuration")
    raw = value.get_secret_value()
    if not 1 <= len(raw) <= 4096 or any(
        unicodedata.category(character) == "Cc" for character in raw
    ):
        raise ConfigurationError("invalid_configuration")
    return value


class CredentialService:
    def __init__(self, catalog: ConnectionCatalog, secret_store: SecretStore) -> None:
        self._catalog = catalog
        self._secret_store = secret_store

    def save_managed(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        alias: str,
        value: SecretStr,
        *,
        replaces_ref: CredentialRef | None = None,
    ) -> CredentialWrite:
        secret = _validate_managed_secret(value)
        credential = CredentialMetadata(
            ref=CredentialRef(workspace_id, uuid4()),
            alias=alias,
            source="macos_keychain",
            status="pending",
            env_name=None,
            replaces_ref=replaces_ref,
            created_at=datetime.now(UTC),
            version=1,
        )
        requested = CredentialWrite(
            workspace_id=workspace_id,
            operation_id=operation_id,
            credential=credential,
            status="pending",
            failure_code=None,
        )

        try:
            reserved = self._catalog.reserve_credential_write(requested)
        except ConfigurationError as exc:
            if exc.code != "credential_write_unknown":
                raise ConfigurationError(exc.code) from None
            reserved = self._lookup_write(workspace_id, operation_id)
            if reserved is None:
                raise ConfigurationError("credential_write_unknown") from None
            if not _same_operation_metadata(reserved.credential, credential):
                raise ConfigurationError("credential_operation_conflict") from None
            return reserved
        except Exception:  # noqa: BLE001
            reserved = self._lookup_write(workspace_id, operation_id)
            if reserved is None:
                raise ConfigurationError("credential_write_unknown") from None
            if not _same_operation_metadata(reserved.credential, credential):
                raise ConfigurationError("credential_operation_conflict") from None
            return reserved

        if not isinstance(reserved, CredentialWrite):
            raise ConfigurationError("credential_write_unknown")
        if (
            reserved.workspace_id != workspace_id
            or reserved.operation_id != operation_id
        ):
            raise ConfigurationError("credential_write_unknown")
        if not _same_operation_metadata(reserved.credential, credential):
            raise ConfigurationError("credential_operation_conflict")
        if reserved.credential.ref != credential.ref or reserved.status != "pending":
            return reserved

        try:
            self._secret_store.put_new(credential.ref, secret)
        except ConfigurationError as exc:
            status: Literal["failed", "unknown"] = (
                "unknown" if exc.code == "credential_write_unknown" else "failed"
            )
        except Exception:  # noqa: BLE001
            status = "unknown"
        else:
            status = "completed"
        return self._finish_write(workspace_id, operation_id, status)

    def register_env(
        self,
        workspace_id: UUID,
        alias: str,
        env_name: str,
    ) -> CredentialMetadata:
        credential = CredentialMetadata(
            ref=CredentialRef(workspace_id, uuid4()),
            alias=alias,
            source="env",
            status="active",
            env_name=env_name,
            replaces_ref=None,
            created_at=datetime.now(UTC),
            version=1,
        )
        try:
            self._catalog.create_env_credential(credential)
        except ConfigurationError as exc:
            if exc.code != "credential_write_unknown":
                raise ConfigurationError(exc.code) from None
            return self._recover_env_registration(credential)
        except Exception:  # noqa: BLE001
            return self._recover_env_registration(credential)
        return credential

    def recover_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
    ) -> CredentialWrite:
        write = self._lookup_write(workspace_id, operation_id)
        if write is None:
            raise ConfigurationError("credential_write_unknown")
        if write.status in {"completed", "failed"}:
            return write

        try:
            self._secret_store.read(write.credential.ref)
        except ConfigurationError as exc:
            if exc.code == "credential_unresolved":
                if write.status == "pending":
                    current = self._lookup_write(workspace_id, operation_id)
                    if current is None:
                        raise ConfigurationError("credential_write_unknown") from None
                    return current
                return self._finish_write(workspace_id, operation_id, "failed")
            raise ConfigurationError(exc.code) from None
        except Exception:  # noqa: BLE001
            raise ConfigurationError("credential_store_unavailable") from None
        return self._finish_write(workspace_id, operation_id, "completed")

    def revoke(
        self,
        ref: CredentialRef,
        *,
        expected_version: int,
    ) -> CredentialRevocationResult:
        if (
            not isinstance(ref, CredentialRef)
            or type(expected_version) is not int
            or expected_version < 1
        ):
            raise ConfigurationError("invalid_configuration")
        try:
            credential = self._catalog.revoke_credential(
                ref, expected_version=expected_version
            )
        except ConfigurationError as exc:
            if exc.code != "credential_write_unknown":
                raise ConfigurationError(exc.code) from None
            credential = self._recover_revocation(ref, expected_version)
        except Exception:  # noqa: BLE001
            credential = self._recover_revocation(ref, expected_version)

        if (
            not isinstance(credential, CredentialMetadata)
            or credential.ref != ref
            or credential.status != "revoked"
            or credential.version != expected_version + 1
        ):
            raise ConfigurationError("credential_write_unknown")
        if credential.source == "env":
            return CredentialRevocationResult(credential, cleanup_pending=False)
        try:
            self._secret_store.delete_owned(ref)
        except ConfigurationError as exc:
            if exc.code == "credential_unresolved":
                return CredentialRevocationResult(credential, cleanup_pending=False)
            return CredentialRevocationResult(credential, cleanup_pending=True)
        except Exception:  # noqa: BLE001
            return CredentialRevocationResult(credential, cleanup_pending=True)
        return CredentialRevocationResult(credential, cleanup_pending=False)

    def retry_revoked_cleanup(self, ref: CredentialRef) -> None:
        if not isinstance(ref, CredentialRef):
            raise ConfigurationError("invalid_configuration")
        try:
            credential = self._catalog.get_credential(ref)
        except ConfigurationError as exc:
            raise ConfigurationError(exc.code) from None
        except Exception:  # noqa: BLE001
            raise ConfigurationError("credential_write_unknown") from None
        if not isinstance(credential, CredentialMetadata) or credential.ref != ref:
            raise ConfigurationError("credential_write_unknown")
        if credential.status != "revoked":
            raise ConfigurationError("credential_operation_conflict")
        if credential.source == "env":
            return
        try:
            self._secret_store.delete_owned(ref)
        except ConfigurationError as exc:
            if exc.code == "credential_unresolved":
                return
            raise ConfigurationError(exc.code) from None
        except Exception:  # noqa: BLE001
            raise ConfigurationError("credential_store_unavailable") from None

    def _lookup_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
    ) -> CredentialWrite | None:
        try:
            write = self._catalog.get_credential_write(workspace_id, operation_id)
        except ConfigurationError as exc:
            if exc.code == "credential_write_unknown":
                return None
            raise ConfigurationError(exc.code) from None
        except Exception:  # noqa: BLE001
            raise ConfigurationError("credential_write_unknown") from None
        if (
            not isinstance(write, CredentialWrite)
            or write.workspace_id != workspace_id
            or write.operation_id != operation_id
        ):
            raise ConfigurationError("credential_write_unknown")
        return write

    def _finish_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        status: Literal["completed", "failed", "unknown"],
    ) -> CredentialWrite:
        try:
            write = self._catalog.finish_credential_write(
                workspace_id, operation_id, status=status
            )
        except Exception:  # noqa: BLE001
            write = self._lookup_write(workspace_id, operation_id)
            if write is None:
                raise ConfigurationError("credential_write_unknown") from None
            return write
        if (
            not isinstance(write, CredentialWrite)
            or write.workspace_id != workspace_id
            or write.operation_id != operation_id
        ):
            raise ConfigurationError("credential_write_unknown")
        return write

    def _recover_env_registration(
        self,
        requested: CredentialMetadata,
    ) -> CredentialMetadata:
        try:
            stored = self._catalog.get_credential(requested.ref)
        except Exception:  # noqa: BLE001
            raise ConfigurationError("credential_write_unknown") from None
        if (
            not isinstance(stored, CredentialMetadata)
            or stored.ref != requested.ref
            or stored.status != "active"
            or not _same_operation_metadata(stored, requested)
        ):
            raise ConfigurationError("credential_already_exists")
        return stored

    def _recover_revocation(
        self,
        ref: CredentialRef,
        expected_version: int,
    ) -> CredentialMetadata:
        try:
            credential = self._catalog.get_credential(ref)
        except Exception:  # noqa: BLE001
            raise ConfigurationError("credential_write_unknown") from None
        if (
            not isinstance(credential, CredentialMetadata)
            or credential.ref != ref
            or credential.status != "revoked"
            or credential.version != expected_version + 1
        ):
            raise ConfigurationError("credential_write_unknown")
        return credential
