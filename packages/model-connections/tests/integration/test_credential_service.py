from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sidera_model_connections.contracts import (
    ConnectionRef,
    CredentialRef,
    CredentialWrite,
    CredentialWriteStatus,
    ModelConnectionRevision,
)
from sidera_model_connections.credentials import CredentialService
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.postgres import PostgresConnectionCatalog
from sidera_model_connections.postgres.models import (
    CredentialEventRow,
    CredentialWriteRow,
)
from tests.fakes import FakeSecretStore

SYNTHETIC_KEY = "cfg2-fake-integration-key"


class FinishAcknowledgementLostCatalog:
    def __init__(self, catalog: PostgresConnectionCatalog) -> None:
        self._catalog = catalog
        self._lose_acknowledgement = True

    def __getattr__(self, name: str):
        return getattr(self._catalog, name)

    def finish_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        *,
        status: CredentialWriteStatus,
    ) -> CredentialWrite:
        result = self._catalog.finish_credential_write(
            workspace_id, operation_id, status=status
        )
        if status == "completed" and self._lose_acknowledgement:
            self._lose_acknowledgement = False
            raise RuntimeError(f"database acknowledgement lost {SYNTHETIC_KEY}")
        return result


class FailingDeleteStore(FakeSecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_delete = True
        self.status_at_delete: list[str] = []
        self.catalog: PostgresConnectionCatalog | None = None

    def delete_owned(self, ref: CredentialRef) -> None:
        self.calls.append(("delete_owned", ref))
        if self.catalog is not None:
            self.status_at_delete.append(self.catalog.get_credential(ref).status)
        if self.fail_delete:
            raise RuntimeError("delete acknowledgement unavailable")
        self._values.pop(ref, None)


class ReentrantRecoveryStore(FakeSecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.before_store: Callable[[CredentialRef], None] | None = None
        self.during_put: list[tuple[str, str, str]] = []

    def put_new(self, ref: CredentialRef, value: SecretStr) -> None:
        self.calls.append(("put_new", ref))
        if self.before_store is not None:
            self.before_store(ref)
        self._values[ref] = value

    def has_item(self, ref: CredentialRef) -> bool:
        return ref in self._values


def captured(call):
    try:
        return call(), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


def test_postgres_operation_recovery_and_repeat_save_use_one_original_identity(
    catalog: PostgresConnectionCatalog,
    workspace_id: UUID,
    session_factory: sessionmaker[Session],
) -> None:
    store = FakeSecretStore()
    service = CredentialService(FinishAcknowledgementLostCatalog(catalog), store)
    operation_id = uuid4()

    result, _error = captured(
        lambda: service.save_managed(
            workspace_id, operation_id, "managed", SecretStr(SYNTHETIC_KEY)
        )
    )
    assert isinstance(result, CredentialWrite)
    repeated = service.save_managed(
        workspace_id, operation_id, "managed", SecretStr("cfg2-fake-other-key")
    )

    assert result.status == "completed"
    assert repeated == result
    assert repeated.credential.status == "active"
    assert sum(action == "put_new" for action, _ref in store.calls) == 1
    assert SYNTHETIC_KEY not in repr(repeated)
    assert SYNTHETIC_KEY not in repr(store.calls)
    with session_factory() as session:
        stored_write = session.scalar(
            select(CredentialWriteRow).where(
                CredentialWriteRow.workspace_id == workspace_id,
                CredentialWriteRow.operation_id == operation_id,
            )
        )
        events = session.scalars(
            select(CredentialEventRow.type).where(
                CredentialEventRow.workspace_id == workspace_id,
                CredentialEventRow.credential_id
                == result.credential.ref.credential_id,
            )
        ).all()
    assert stored_write is not None
    assert stored_write.credential_id == result.credential.ref.credential_id
    assert stored_write.status == "completed"
    assert events == ["created"]

    with pytest.raises(ConfigurationError) as conflict:
        service.save_managed(
            workspace_id, operation_id, "different alias", SecretStr("cfg2-fake-next")
        )
    assert conflict.value.code == "credential_operation_conflict"
    assert sum(action == "put_new" for action, _ref in store.calls) == 1


def test_postgres_pending_missing_callback_does_not_poison_original_put(
    catalog: PostgresConnectionCatalog,
    workspace_id: UUID,
    session_factory: sessionmaker[Session],
) -> None:
    store = ReentrantRecoveryStore()
    service = CredentialService(catalog, store)
    operation_id = uuid4()

    def recover_before_store(ref: CredentialRef) -> None:
        reserved = catalog.get_credential_write(workspace_id, operation_id)
        recovered = service.recover_write(workspace_id, operation_id)
        current = catalog.get_credential_write(workspace_id, operation_id)
        assert reserved.credential.ref == ref
        store.during_put.append((reserved.status, recovered.status, current.status))

    store.before_store = recover_before_store
    result = service.save_managed(
        workspace_id, operation_id, "managed", SecretStr(SYNTHETIC_KEY)
    )
    current_write = catalog.get_credential_write(workspace_id, operation_id)
    current_credential = catalog.get_credential(result.credential.ref)
    with session_factory() as session:
        events = session.scalars(
            select(CredentialEventRow.type).where(
                CredentialEventRow.workspace_id == workspace_id,
                CredentialEventRow.credential_id
                == result.credential.ref.credential_id,
            )
        ).all()

    observed = (
        store.during_put,
        result.status,
        current_write.status,
        current_credential.status,
        store.has_item(result.credential.ref),
        sum(action == "put_new" for action, _ref in store.calls),
        events,
    )
    expected = (
        [("pending", "pending", "pending")],
        "completed",
        "completed",
        "active",
        True,
        1,
        ["created"],
    )
    assert observed == expected, f"safe lifecycle observation: {observed!r}"
    assert [ref for action, ref in store.calls if action == "put_new"] == [
        result.credential.ref
    ]
    assert SYNTHETIC_KEY not in repr(observed)


def test_postgres_rotation_keeps_old_binding_and_credential_active(
    catalog: PostgresConnectionCatalog,
    workspace_id: UUID,
) -> None:
    store = FakeSecretStore()
    service = CredentialService(catalog, store)
    first = service.save_managed(
        workspace_id, uuid4(), "managed", SecretStr(SYNTHETIC_KEY)
    )
    connection = ModelConnectionRevision(
        ref=ConnectionRef(workspace_id, uuid4(), 1),
        name="original binding",
        provider_id="example",
        protocol="openai_compatible",
        base_url="https://api.example.test/v1",
        network_scope="public",
        allowed_roles=frozenset({"worker"}),
        credential_ref=first.credential.ref,
        created_at=datetime.now(UTC),
    )
    catalog.create_connection(connection)

    rotated = service.save_managed(
        workspace_id,
        uuid4(),
        "managed",
        SecretStr("cfg2-fake-rotated-key"),
        replaces_ref=first.credential.ref,
    )

    assert rotated.status == "completed"
    assert rotated.credential.replaces_ref == first.credential.ref
    assert catalog.get_credential(first.credential.ref) == first.credential
    assert catalog.get_connection(connection.ref) == connection
    assert catalog.list_connections(workspace_id) == (connection,)
    assert sum(action == "put_new" for action, _ref in store.calls) == 2
    assert not any(action == "delete_owned" for action, _ref in store.calls)


def test_postgres_revoke_commits_cas_before_failed_cleanup(
    catalog: PostgresConnectionCatalog,
    workspace_id: UUID,
    session_factory: sessionmaker[Session],
) -> None:
    store = FailingDeleteStore()
    service = CredentialService(catalog, store)
    store.catalog = catalog
    operation_id = uuid4()
    saved = service.save_managed(
        workspace_id, operation_id, "managed", SecretStr(SYNTHETIC_KEY)
    )

    revoked = service.revoke(
        saved.credential.ref,
        expected_version=saved.credential.version,
    )

    assert revoked.credential.status == "revoked"
    assert revoked.credential.version == saved.credential.version + 1
    assert revoked.cleanup_pending is True
    assert store.status_at_delete == ["revoked"]
    assert catalog.get_credential(saved.credential.ref) == revoked.credential
    repeat_request = CredentialWrite(
        workspace_id=workspace_id,
        operation_id=operation_id,
        credential=replace(saved.credential, status="pending", version=1),
        status="pending",
        failure_code=None,
    )
    hydrated_reads = (
        catalog.get_credential_write(workspace_id, operation_id),
        catalog.reserve_credential_write(repeat_request),
        catalog.finish_credential_write(
            workspace_id, operation_id, status="completed"
        ),
    )
    assert all(write.status == "completed" for write in hydrated_reads)
    assert all(write.credential == revoked.credential for write in hydrated_reads)
    assert all(write.credential.ref == saved.credential.ref for write in hydrated_reads)
    with pytest.raises(ConfigurationError) as conflict:
        service.revoke(
            saved.credential.ref,
            expected_version=saved.credential.version,
        )
    assert conflict.value.code == "configuration_version_conflict"
    with session_factory() as session:
        events = session.scalars(
            select(CredentialEventRow.type).where(
                CredentialEventRow.workspace_id == workspace_id,
                CredentialEventRow.credential_id
                == saved.credential.ref.credential_id,
            )
        ).all()
    assert events == ["created", "revoked"]

    store.fail_delete = False
    service.retry_revoked_cleanup(saved.credential.ref)
    assert catalog.get_credential(saved.credential.ref) == revoked.credential
    assert [ref for action, ref in store.calls if action == "delete_owned"] == [
        saved.credential.ref,
        saved.credential.ref,
    ]
