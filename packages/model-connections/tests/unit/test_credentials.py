import hmac
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from sidera_model_connections.contracts import (
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    CredentialWrite,
    ModelConnectionRevision,
)
from sidera_model_connections.credentials import CredentialService
from sidera_model_connections.errors import ConfigurationError
from tests.fakes import FakeSecretStore, MemoryCatalog

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
SYNTHETIC_KEY = "cfg2-fake-main-key"


def managed_metadata(
    *,
    credential_id: UUID | None = None,
    alias: str = "main",
    replaces_ref: CredentialRef | None = None,
) -> CredentialMetadata:
    return CredentialMetadata(
        ref=CredentialRef(WORKSPACE_ID, credential_id or uuid4()),
        alias=alias,
        source="macos_keychain",
        status="pending",
        env_name=None,
        replaces_ref=replaces_ref,
        created_at=datetime.now(UTC),
        version=1,
    )


def pending_write(
    metadata: CredentialMetadata,
    *,
    operation_id: UUID | None = None,
) -> CredentialWrite:
    return CredentialWrite(
        workspace_id=WORKSPACE_ID,
        operation_id=operation_id or uuid4(),
        credential=metadata,
        status="pending",
        failure_code=None,
    )


def secret_matches(value: SecretStr, expected: str) -> bool:
    return hmac.compare_digest(value.get_secret_value(), expected)


def capture(call):
    try:
        return call(), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


def is_safe_error(error: Exception | None, code: str) -> bool:
    return (
        isinstance(error, ConfigurationError)
        and error.code == code
        and SYNTHETIC_KEY not in str(error)
        and SYNTHETIC_KEY not in repr(error)
    )


def build_service(catalog=None, store=None):
    catalog = MemoryCatalog() if catalog is None else catalog
    store = FakeSecretStore() if store is None else store
    return CredentialService(catalog, store), catalog, store


class PutUnknownAfterWriteStore(FakeSecretStore):
    def put_new(self, ref: CredentialRef, value: SecretStr) -> None:
        super().put_new(ref, value)
        raise ConfigurationError("credential_write_unknown")


class KnownPutFailureStore(FakeSecretStore):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    def put_new(self, ref: CredentialRef, value: SecretStr) -> None:
        self.calls.append(("put_new", ref))
        raise ConfigurationError(self.code)


class LockedReadStore(FakeSecretStore):
    def read(self, ref: CredentialRef) -> SecretStr:
        self.calls.append(("read", ref))
        raise ConfigurationError("credential_store_locked")


class ReserveAcknowledgementLostCatalog(MemoryCatalog):
    def reserve_credential_write(self, write: CredentialWrite) -> CredentialWrite:
        super().reserve_credential_write(write)
        raise RuntimeError(f"database acknowledgement lost {SYNTHETIC_KEY}")


class ReserveBeforeCommitCatalog(MemoryCatalog):
    def reserve_credential_write(self, write: CredentialWrite) -> CredentialWrite:
        raise RuntimeError(f"database unavailable {SYNTHETIC_KEY}")


class FinishAcknowledgementLostCatalog(MemoryCatalog):
    lose_acknowledgement = True

    def finish_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        *,
        status,
    ) -> CredentialWrite:
        result = super().finish_credential_write(
            workspace_id, operation_id, status=status
        )
        if status == "completed" and self.lose_acknowledgement:
            self.lose_acknowledgement = False
            raise RuntimeError(f"database acknowledgement lost {SYNTHETIC_KEY}")
        return result


class FinishBeforeCommitCatalog(MemoryCatalog):
    fail_completion = True

    def finish_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        *,
        status,
    ) -> CredentialWrite:
        if status == "completed" and self.fail_completion:
            self.fail_completion = False
            raise RuntimeError(f"database unavailable {SYNTHETIC_KEY}")
        return super().finish_credential_write(
            workspace_id, operation_id, status=status
        )


class ToggleDeleteFailureStore(FakeSecretStore):
    fail_delete = True
    before_delete = None

    def delete_owned(self, ref: CredentialRef) -> None:
        if self.before_delete is not None:
            self.before_delete(ref)
        if self.fail_delete:
            self.calls.append(("delete_owned", ref))
            raise RuntimeError("delete acknowledgement unavailable")
        super().delete_owned(ref)


class ReentrantRecoveryStore(FakeSecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.before_store: Callable[[CredentialRef], None] | None = None

    def put_new(self, ref: CredentialRef, value: SecretStr) -> None:
        self.calls.append(("put_new", ref))
        if self.before_store is not None:
            self.before_store(ref)
        self._values[ref] = value

    def has_item(self, ref: CredentialRef) -> bool:
        return ref in self._values


def test_memory_catalog_repeat_reserve_returns_original_identity() -> None:
    catalog = MemoryCatalog()
    operation_id = UUID("20000000-0000-0000-0000-000000000001")
    first = pending_write(managed_metadata(), operation_id=operation_id)

    reserved = catalog.reserve_credential_write(first)
    try:
        repeated = catalog.reserve_credential_write(
            pending_write(
                replace(first.credential, ref=CredentialRef(WORKSPACE_ID, uuid4())),
                operation_id=operation_id,
            )
        )
    except ConfigurationError as exc:
        pytest.fail(f"repeat reserve rejected with {exc.code}")

    assert repeated == reserved
    assert repeated.credential.ref == first.credential.ref
    assert repeated.status == "pending"
    assert catalog.get_credential(first.credential.ref) == first.credential
    assert getattr(catalog, "events", ()) == []


def test_memory_catalog_rejects_different_metadata_for_same_operation() -> None:
    catalog = MemoryCatalog()
    operation_id = UUID("20000000-0000-0000-0000-000000000002")
    first = pending_write(managed_metadata(), operation_id=operation_id)
    catalog.reserve_credential_write(first)

    with pytest.raises(ConfigurationError) as rejected:
        catalog.reserve_credential_write(
            pending_write(
                managed_metadata(alias="other"), operation_id=operation_id
            )
        )

    assert rejected.value.code == "credential_operation_conflict"


def test_memory_catalog_finish_updates_metadata_and_emits_only_transitions() -> None:
    catalog = MemoryCatalog()
    write = pending_write(managed_metadata())
    catalog.reserve_credential_write(write)

    unknown = catalog.finish_credential_write(
        WORKSPACE_ID, write.operation_id, status="unknown"
    )
    assert unknown.status == "unknown"
    assert unknown.credential.status == "pending"
    assert unknown.credential.version == 1
    assert [event[0] for event in getattr(catalog, "events", ())] == [
        "write_unknown"
    ]

    catalog.finish_credential_write(
        WORKSPACE_ID, write.operation_id, status="unknown"
    )
    completed = catalog.finish_credential_write(
        WORKSPACE_ID, write.operation_id, status="completed"
    )

    assert completed.status == "completed"
    assert completed.credential.status == "active"
    assert completed.credential.version == 2
    assert catalog.get_credential(write.credential.ref) == completed.credential
    assert [event[0] for event in getattr(catalog, "events", ())] == [
        "write_unknown",
        "created",
    ]


def test_memory_catalog_lists_heads_and_revokes_with_version_cas() -> None:
    catalog = MemoryCatalog()
    credential = CredentialMetadata(
        ref=CredentialRef(WORKSPACE_ID, UUID(int=50)),
        alias="environment",
        source="env",
        status="active",
        env_name="SIDERA_TEST_KEY",
        replaces_ref=None,
        created_at=datetime.now(UTC),
        version=1,
    )
    catalog.create_env_credential(credential)
    connection_id = UUID(int=60)
    connection = ModelConnectionRevision(
        ref=ConnectionRef(WORKSPACE_ID, connection_id, 1),
        name="Connection v1",
        provider_id="example",
        protocol="openai_compatible",
        base_url="https://api.example.test/v1",
        network_scope="public",
        allowed_roles=frozenset({"worker"}),
        credential_ref=credential.ref,
        created_at=datetime.now(UTC),
    )
    catalog.create_connection(connection)
    latest = replace(connection, ref=ConnectionRef(WORKSPACE_ID, connection_id, 2))
    catalog.append_connection(latest, expected_revision=1)

    revoked = catalog.revoke_credential(credential.ref, expected_version=1)

    assert catalog.list_connections(WORKSPACE_ID) == (latest,)
    assert revoked.status == "revoked"
    assert revoked.version == 2
    assert catalog.get_credential(credential.ref) == revoked
    assert [event[0] for event in getattr(catalog, "events", ())] == [
        "created",
        "revoked",
    ]
    with pytest.raises(ConfigurationError) as conflict:
        catalog.revoke_credential(credential.ref, expected_version=1)
    assert conflict.value.code == "configuration_version_conflict"


def test_repeat_save_returns_original_write_without_a_second_key_write() -> None:
    service, _catalog, store = build_service()
    operation_id = UUID("30000000-0000-0000-0000-000000000001")

    first = service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
    )
    second = service.save_managed(
        WORKSPACE_ID,
        operation_id,
        "main",
        SecretStr("cfg2-fake-other-key"),
    )

    assert second == first
    assert sum(action == "put_new" for action, _ref in store.calls) == 1
    assert SYNTHETIC_KEY not in repr(first)
    assert SYNTHETIC_KEY not in repr(store.calls)


def test_reusing_operation_with_different_metadata_conflicts() -> None:
    service, _catalog, store = build_service()
    operation_id = UUID("30000000-0000-0000-0000-000000000002")
    service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
    )

    with pytest.raises(ConfigurationError) as rejected:
        service.save_managed(
            WORKSPACE_ID, operation_id, "other", SecretStr("cfg2-fake-other-key")
        )

    assert rejected.value.code == "credential_operation_conflict"
    assert sum(action == "put_new" for action, _ref in store.calls) == 1


def test_unknown_vault_write_is_not_replayed_and_recovers_by_original_ref() -> None:
    service, catalog, store = build_service(store=PutUnknownAfterWriteStore())
    operation_id = UUID("30000000-0000-0000-0000-000000000003")

    unknown = service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
    )
    repeated = service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr("cfg2-fake-other-key")
    )
    recovered = service.recover_write(WORKSPACE_ID, operation_id)

    assert unknown.status == "unknown"
    assert repeated == unknown
    assert recovered.status == "completed"
    assert recovered.credential.status == "active"
    assert sum(action == "put_new" for action, _ref in store.calls) == 1
    assert store.calls[-1] == ("read", recovered.credential.ref)
    assert secret_matches(store.read(recovered.credential.ref), SYNTHETIC_KEY)
    assert catalog.get_credential(recovered.credential.ref) == recovered.credential
    assert SYNTHETIC_KEY not in repr(catalog.events)


def test_pending_missing_recovery_cannot_poison_reentrant_original_put() -> None:
    catalog = MemoryCatalog()
    store = ReentrantRecoveryStore()
    service = CredentialService(catalog, store)
    operation_id = uuid4()
    recovery_during_put: list[tuple[str, bool]] = []
    store.before_store = lambda ref: recovery_during_put.append(
        (
            service.recover_write(WORKSPACE_ID, operation_id).status,
            catalog.get_credential_write(WORKSPACE_ID, operation_id).credential.ref
            == ref,
        )
    )

    result = service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
    )
    current_write = catalog.get_credential_write(WORKSPACE_ID, operation_id)
    current_credential = catalog.get_credential(result.credential.ref)
    observed = (
        recovery_during_put,
        current_write.status,
        current_credential.status,
        store.has_item(result.credential.ref),
        sum(action == "put_new" for action, _ref in store.calls),
        tuple(event_type for event_type, _ref, _operation in catalog.events),
    )

    expected = (
        [("pending", True)],
        "completed",
        "active",
        True,
        1,
        ("created",),
    )
    assert observed == expected, f"safe lifecycle observation: {observed!r}"
    assert [ref for action, ref in store.calls if action == "put_new"] == [
        result.credential.ref
    ]


@pytest.mark.parametrize(
    "code",
    ["credential_store_locked", "credential_store_unavailable"],
    ids=["locked", "unavailable"],
)
def test_known_vault_refusal_marks_write_failed(code: str) -> None:
    service, catalog, store = build_service(store=KnownPutFailureStore(code))
    operation_id = uuid4()

    failed = service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
    )

    assert failed.status == "failed"
    assert failed.credential.status == "failed"
    assert catalog.get_credential(failed.credential.ref).status == "failed"
    assert sum(action == "put_new" for action, _ref in store.calls) == 1
    assert SYNTHETIC_KEY not in repr(failed)
    assert SYNTHETIC_KEY not in repr(catalog.events)


def test_pending_recovery_of_missing_item_retains_original_write() -> None:
    service, catalog, store = build_service()
    write = pending_write(managed_metadata())
    catalog.reserve_credential_write(write)

    pending = service.recover_write(WORKSPACE_ID, write.operation_id)

    assert pending.status == "pending"
    assert pending.credential.ref == write.credential.ref
    assert catalog.get_credential_write(WORKSPACE_ID, write.operation_id).status == "pending"
    assert catalog.get_credential(write.credential.ref).status == "pending"
    assert catalog.events == []
    assert store.calls == [("read", write.credential.ref)]


def test_unknown_recovery_of_definitely_missing_item_marks_original_write_failed() -> None:
    service, catalog, store = build_service()
    write = pending_write(managed_metadata())
    catalog.reserve_credential_write(write)
    catalog.finish_credential_write(
        WORKSPACE_ID, write.operation_id, status="unknown"
    )

    failed = service.recover_write(WORKSPACE_ID, write.operation_id)

    assert failed.status == "failed"
    assert failed.credential.ref == write.credential.ref
    assert catalog.get_credential(write.credential.ref).status == "failed"
    assert catalog.events == [
        ("write_unknown", write.credential.ref, write.operation_id),
        ("write_failed", write.credential.ref, write.operation_id),
    ]
    assert store.calls == [("read", write.credential.ref)]


def test_memory_catalog_write_reads_hydrate_current_revoked_metadata() -> None:
    service, catalog, _store = build_service()
    operation_id = uuid4()
    completed = service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
    )
    revoked = catalog.revoke_credential(
        completed.credential.ref,
        expected_version=completed.credential.version,
    )

    queried = catalog.get_credential_write(WORKSPACE_ID, operation_id)
    repeated = catalog.reserve_credential_write(completed)
    same_status = catalog.finish_credential_write(
        WORKSPACE_ID, operation_id, status="completed"
    )

    observed = (queried, repeated, same_status)
    assert all(write.status == "completed" for write in observed)
    assert all(write.credential.ref == revoked.ref for write in observed)
    assert all(write.credential == revoked for write in observed)


def test_locked_recovery_does_not_treat_unavailable_read_as_missing() -> None:
    service, catalog, store = build_service(store=LockedReadStore())
    write = pending_write(managed_metadata())
    catalog.reserve_credential_write(write)

    _result, error = capture(
        lambda: service.recover_write(WORKSPACE_ID, write.operation_id)
    )

    assert is_safe_error(error, "credential_store_locked")
    assert catalog.get_credential_write(WORKSPACE_ID, write.operation_id).status == "pending"
    assert catalog.get_credential(write.credential.ref).status == "pending"
    assert store.calls == [("read", write.credential.ref)]


def test_unknown_reserve_after_commit_queries_original_operation_without_vault_write() -> None:
    catalog = ReserveAcknowledgementLostCatalog()
    service, catalog, store = build_service(catalog=catalog)
    operation_id = UUID("30000000-0000-0000-0000-000000000004")

    result = service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
    )

    assert result.status == "pending"
    assert catalog.get_credential_write(WORKSPACE_ID, operation_id) == result
    assert store.calls == []
    assert SYNTHETIC_KEY not in repr(result)


def test_unknown_reserve_before_commit_never_retries_or_writes_secret() -> None:
    catalog = ReserveBeforeCommitCatalog()
    service, catalog, store = build_service(catalog=catalog)
    operation_id = UUID("30000000-0000-0000-0000-000000000005")

    _result, error = capture(
        lambda: service.save_managed(
            WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
        )
    )

    assert is_safe_error(error, "credential_write_unknown")
    assert catalog.credential_writes == {}
    assert store.calls == []


def test_unknown_finish_after_commit_queries_original_operation() -> None:
    catalog = FinishAcknowledgementLostCatalog()
    service, catalog, store = build_service(catalog=catalog)
    operation_id = UUID("30000000-0000-0000-0000-000000000006")

    result = service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
    )

    assert result.status == "completed"
    assert catalog.get_credential_write(WORKSPACE_ID, operation_id) == result
    assert catalog.get_credential(result.credential.ref).status == "active"
    assert sum(action == "put_new" for action, _ref in store.calls) == 1
    assert SYNTHETIC_KEY not in repr(result)
    assert SYNTHETIC_KEY not in repr(catalog.events)


def test_unknown_finish_before_commit_recovers_existing_vault_item_without_rewrite() -> None:
    catalog = FinishBeforeCommitCatalog()
    service, catalog, store = build_service(catalog=catalog)
    operation_id = UUID("30000000-0000-0000-0000-000000000007")

    pending = service.save_managed(
        WORKSPACE_ID, operation_id, "main", SecretStr(SYNTHETIC_KEY)
    )
    recovered = service.recover_write(WORKSPACE_ID, operation_id)

    assert pending.status == "pending"
    assert recovered.status == "completed"
    assert recovered.credential.ref == pending.credential.ref
    assert sum(action == "put_new" for action, _ref in store.calls) == 1
    assert store.calls[-1] == ("read", pending.credential.ref)


def test_rotation_keeps_old_credential_and_binding_active() -> None:
    service, catalog, store = build_service()
    first = service.save_managed(
        WORKSPACE_ID, uuid4(), "main", SecretStr(SYNTHETIC_KEY)
    )
    connection = ModelConnectionRevision(
        ref=ConnectionRef(WORKSPACE_ID, UUID(int=70), 1),
        name="bound to original credential",
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
        WORKSPACE_ID,
        uuid4(),
        "main",
        SecretStr("cfg2-fake-rotated-key"),
        replaces_ref=first.credential.ref,
    )

    assert rotated.credential.ref != first.credential.ref
    assert rotated.credential.replaces_ref == first.credential.ref
    assert catalog.get_credential(first.credential.ref) == first.credential
    assert catalog.get_connection(connection.ref) == connection
    assert secret_matches(store.read(first.credential.ref), SYNTHETIC_KEY)
    assert sum(action == "put_new" for action, _ref in store.calls) == 2
    assert not any(action == "delete_owned" for action, _ref in store.calls)


def test_env_registration_records_name_without_reading_environment_or_vault() -> None:
    service, catalog, store = build_service()

    credential = service.register_env(WORKSPACE_ID, "deployment", "SIDERA_TEST_KEY")

    assert credential.source == "env"
    assert credential.status == "active"
    assert credential.env_name == "SIDERA_TEST_KEY"
    assert catalog.get_credential(credential.ref) == credential
    assert store.calls == []


def test_revoke_commits_before_delete_and_retry_never_restores_active() -> None:
    store = ToggleDeleteFailureStore()
    service, catalog, store = build_service(store=store)
    saved = service.save_managed(
        WORKSPACE_ID, uuid4(), "main", SecretStr(SYNTHETIC_KEY)
    )

    def verify_revoked_before_delete(ref: CredentialRef) -> None:
        assert catalog.get_credential(ref).status == "revoked"

    store.before_delete = verify_revoked_before_delete
    revoked = service.revoke(
        saved.credential.ref, expected_version=saved.credential.version
    )

    assert revoked.credential.status == "revoked"
    assert revoked.credential.version == saved.credential.version + 1
    assert revoked.cleanup_pending is True
    assert catalog.get_credential(saved.credential.ref) == revoked.credential
    assert catalog.events[-1][0] == "revoked"
    assert SYNTHETIC_KEY not in repr(revoked)

    _result, error = capture(
        lambda: service.retry_revoked_cleanup(saved.credential.ref)
    )
    assert is_safe_error(error, "credential_store_unavailable")
    assert catalog.get_credential(saved.credential.ref) == revoked.credential

    store.fail_delete = False
    service.retry_revoked_cleanup(saved.credential.ref)
    assert catalog.get_credential(saved.credential.ref) == revoked.credential
    assert [ref for action, ref in store.calls if action == "delete_owned"] == [
        saved.credential.ref,
        saved.credential.ref,
        saved.credential.ref,
    ]


def test_cleanup_retry_rejects_non_revoked_metadata_without_deleting() -> None:
    service, _catalog, store = build_service()
    credential = service.register_env(WORKSPACE_ID, "deployment", "SIDERA_TEST_KEY")

    with pytest.raises(ConfigurationError) as rejected:
        service.retry_revoked_cleanup(credential.ref)

    assert rejected.value.code == "credential_operation_conflict"
    assert store.calls == []


@pytest.mark.parametrize(
    "value",
    [
        "plain-string",
        SecretStr(""),
        SecretStr("x" * 4097),
        SecretStr("cfg2-fake-line\nbreak"),
    ],
    ids=["not-secretstr", "empty", "too-long", "control-character"],
)
def test_managed_write_rejects_invalid_secret_input(value: object) -> None:
    service, catalog, store = build_service()

    _result, error = capture(
        lambda: service.save_managed(WORKSPACE_ID, uuid4(), "main", value)
    )

    assert is_safe_error(error, "invalid_configuration")
    assert catalog.credential_writes == {}
    assert store.calls == []


@pytest.mark.parametrize(
    "value",
    [SecretStr("x"), SecretStr("x" * 4096)],
    ids=["minimum-length", "maximum-length"],
)
def test_managed_write_accepts_secret_length_boundaries(value: SecretStr) -> None:
    service, _catalog, store = build_service()

    result = service.save_managed(WORKSPACE_ID, uuid4(), "main", value)

    assert result.status == "completed"
    assert sum(action == "put_new" for action, _ref in store.calls) == 1
