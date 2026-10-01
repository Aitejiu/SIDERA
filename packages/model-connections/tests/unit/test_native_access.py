import traceback
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from sidera_model_connections.contracts import CredentialMetadata, CredentialRef
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.native_access import (
    NativeCredentialAccessPermission,
    NativeCredentialAccessRequest,
    NativeCredentialReader,
)
from tests.fakes import FakeSecretStore, MemoryCatalog

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 1, tzinfo=UTC)


class FakePermissionReader:
    def __init__(self, permissions=()) -> None:
        self.permissions = {
            permission.request: permission for permission in permissions
        }
        self.calls: list[NativeCredentialAccessRequest] = []

    def authorize(
        self, request: NativeCredentialAccessRequest
    ) -> NativeCredentialAccessPermission:
        self.calls.append(request)
        permission = self.permissions.get(request)
        if permission is None or permission.request != request:
            raise ConfigurationError("runtime_binding_denied")
        return permission


def _credential(
    ref: CredentialRef,
    *,
    source: str = "macos_keychain",
    status: str = "active",
    env_name: str | None = None,
    version: int = 1,
) -> CredentialMetadata:
    return CredentialMetadata(
        ref=ref,
        alias="native profile credential",
        source=source,
        status=status,
        env_name=env_name,
        replaces_ref=None,
        created_at=NOW,
        version=version,
    )


def _request(
    ref: CredentialRef,
    *,
    scope_id: UUID | None = None,
    destination: str = "SERVICE_API_KEY",
) -> NativeCredentialAccessRequest:
    return NativeCredentialAccessRequest(
        workspace_id=ref.workspace_id,
        credential_ref=ref,
        scope_id=scope_id or uuid4(),
        destination=destination,
    )


def _authorized_reader(catalog, store, request, env_sources=None):
    permissions = FakePermissionReader(
        (NativeCredentialAccessPermission(request=request),)
    )
    reader = NativeCredentialReader(
        catalog,
        permissions,
        store,
        env_sources if env_sources is not None else {},
    )
    return reader, permissions


def test_authorized_role_neutral_request_reads_exact_credential() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    ref = CredentialRef(WORKSPACE_ID, uuid4())
    credential = _credential(ref)
    catalog.credentials[ref] = credential
    store.put_new(ref, SecretStr("fixture-native-credential"))
    store.calls.clear()
    request = _request(ref)
    reader, permissions = _authorized_reader(catalog, store, request)

    value = reader.read(request)

    assert value.get_secret_value() == "fixture-native-credential"
    assert permissions.calls == [request]
    assert store.calls == [("read", ref)]


def test_denied_scope_never_reads_credential_storage() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    ref = CredentialRef(WORKSPACE_ID, uuid4())
    catalog.credentials[ref] = _credential(ref)
    store.put_new(ref, SecretStr("fixture-denied-credential"))
    store.calls.clear()
    request = _request(ref)
    permissions = FakePermissionReader()
    reader = NativeCredentialReader(catalog, permissions, store, {})

    with pytest.raises(ConfigurationError) as denied:
        reader.read(request)

    assert denied.value.code == "runtime_binding_denied"
    assert permissions.calls == [request]
    assert store.calls == []


def test_credential_revocation_is_rechecked_on_every_read() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    ref = CredentialRef(WORKSPACE_ID, uuid4())
    credential = _credential(ref)
    catalog.credentials[ref] = credential
    store.put_new(ref, SecretStr("fixture-revocable-credential"))
    store.calls.clear()
    request = _request(ref)
    reader, permissions = _authorized_reader(catalog, store, request)

    assert reader.read(request).get_secret_value() == "fixture-revocable-credential"
    catalog.credentials[ref] = replace(credential, status="revoked", version=2)

    with pytest.raises(ConfigurationError) as revoked:
        reader.read(request)

    assert revoked.value.code == "credential_revoked"
    assert permissions.calls == [request, request]
    assert store.calls == [("read", ref)]


def test_mismatched_permission_destination_cannot_authorize_read() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    ref = CredentialRef(WORKSPACE_ID, uuid4())
    catalog.credentials[ref] = _credential(ref)
    store.put_new(ref, SecretStr("fixture-wrong-destination"))
    store.calls.clear()
    request = _request(ref, destination="SERVICE_API_KEY")
    permission_request = _request(
        ref,
        scope_id=request.scope_id,
        destination="SERVICE_ACCESS_TOKEN",
    )
    permissions = FakePermissionReader(
        (NativeCredentialAccessPermission(request=permission_request),)
    )
    reader = NativeCredentialReader(catalog, permissions, store, {})

    with pytest.raises(ConfigurationError) as denied:
        reader.read(request)

    assert denied.value.code == "runtime_binding_denied"
    assert permissions.calls == [request]
    assert store.calls == []


def test_mismatched_permission_scope_cannot_authorize_read() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    ref = CredentialRef(WORKSPACE_ID, uuid4())
    catalog.credentials[ref] = _credential(ref)
    store.put_new(ref, SecretStr("fixture-wrong-scope"))
    store.calls.clear()
    request = _request(ref)
    permission_request = _request(ref, destination=request.destination)
    permissions = FakePermissionReader(
        (NativeCredentialAccessPermission(request=permission_request),)
    )
    reader = NativeCredentialReader(catalog, permissions, store, {})

    with pytest.raises(ConfigurationError) as denied:
        reader.read(request)

    assert denied.value.code == "runtime_binding_denied"
    assert permissions.calls == [request]
    assert store.calls == []


class TrackingEnvironment(Mapping[str, SecretStr]):
    def __init__(self, values: dict[str, SecretStr]) -> None:
        self.values = values
        self.lookups: list[str] = []
        self.iterations = 0
        self.length_checks = 0

    def __getitem__(self, key: str) -> SecretStr:
        self.lookups.append(key)
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        return iter(self.values)

    def __len__(self) -> int:
        self.length_checks += 1
        return len(self.values)


def test_environment_credential_reads_only_the_registered_name() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    ref = CredentialRef(WORKSPACE_ID, uuid4())
    catalog.credentials[ref] = _credential(
        ref, source="env", env_name="SIDERA_TEST_KEY"
    )
    request = _request(ref)
    environment = TrackingEnvironment(
        {
            "SIDERA_TEST_KEY": SecretStr("fixture-selected-environment-secret"),
            "OPENAI_API_KEY": SecretStr("fixture-unrelated-environment-secret"),
        }
    )
    reader, permissions = _authorized_reader(catalog, store, request, environment)

    value = reader.read(request)

    assert value.get_secret_value() == "fixture-selected-environment-secret"
    assert permissions.calls == [request]
    assert environment.lookups == ["SIDERA_TEST_KEY"]
    assert environment.iterations == 0
    assert environment.length_checks == 0
    assert store.calls == []


def test_invalid_cross_workspace_request_is_rejected() -> None:
    ref = CredentialRef(UUID(int=999), uuid4())

    with pytest.raises(ConfigurationError) as invalid:
        NativeCredentialAccessRequest(
            workspace_id=WORKSPACE_ID,
            credential_ref=ref,
            scope_id=uuid4(),
            destination="SERVICE_API_KEY",
        )

    assert invalid.value.code == "invalid_configuration"


@pytest.mark.parametrize(
    "destination",
    (
        "header:Authorization",
        "file:/tmp/credential",
        "process:environment",
    ),
)
def test_non_environment_destinations_are_rejected(destination: str) -> None:
    ref = CredentialRef(WORKSPACE_ID, uuid4())

    with pytest.raises(ConfigurationError) as invalid:
        _request(ref, destination=destination)

    assert invalid.value.code == "invalid_configuration"


def test_untrusted_secret_store_error_is_sanitized() -> None:
    catalog = MemoryCatalog()
    ref = CredentialRef(WORKSPACE_ID, uuid4())
    catalog.credentials[ref] = _credential(ref)
    request = _request(ref)
    marker = "fixture-secret-in-storage-error"

    class BrokenStore:
        def read(self, _ref: CredentialRef) -> SecretStr:
            raise RuntimeError(f"private keychain diagnostic: {marker}")

        def put_new(self, _ref: CredentialRef, _value: SecretStr) -> None:
            raise AssertionError("unexpected write")

        def delete_owned(self, _ref: CredentialRef) -> None:
            raise AssertionError("unexpected delete")

    reader, _permissions = _authorized_reader(
        catalog,
        BrokenStore(),
        request,
    )

    with pytest.raises(ConfigurationError) as rejected:
        reader.read(request)

    error = rejected.value
    rendered_traceback = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert error.code == "credential_store_unavailable"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert marker not in "\n".join((str(error), repr(error), rendered_traceback))
