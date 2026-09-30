from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from sidera_model_connections.connections import ConnectionService
from sidera_model_connections.contracts import (
    BindingPermission,
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    ModelConnectionRevision,
    RuntimeAccessRequest,
)
from sidera_model_connections.credentials import CredentialService
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.ports import DenyAllBindingPermissionReader
from sidera_model_connections.resolution import ConnectionResolver
from tests.fakes import (
    FakeBindingPermissionReader,
    FakeSecretStore,
    MemoryCatalog,
)

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
BASE_URL = "https://api.example.test/v1"
ORIGIN = "https://api.example.test"


def _resolver(catalog, permissions, store, env_sources):
    return ConnectionResolver(catalog, permissions, store, env_sources)


def _credential(
    credential_id: UUID,
    *,
    source: str = "macos_keychain",
    status: str = "active",
    env_name: str | None = None,
    alias: str = "runtime key",
) -> CredentialMetadata:
    return CredentialMetadata(
        ref=CredentialRef(WORKSPACE_ID, credential_id),
        alias=alias,
        source=source,
        status=status,
        env_name=env_name,
        replaces_ref=None,
        created_at=datetime.now(UTC),
        version=1,
    )


def _connection(
    connection_id: UUID,
    credential_ref: CredentialRef,
    role: str,
    *,
    revision: int = 1,
    allowed_roles: frozenset[str] | None = None,
) -> ModelConnectionRevision:
    return ModelConnectionRevision(
        ref=ConnectionRef(WORKSPACE_ID, connection_id, revision),
        name="runtime model",
        provider_id="example",
        protocol="openai_compatible",
        base_url=BASE_URL,
        network_scope="public",
        allowed_roles=allowed_roles or frozenset({role}),
        credential_ref=credential_ref,
        created_at=datetime.now(UTC),
    )


def _request(role: str, binding_id: UUID | None = None) -> RuntimeAccessRequest:
    return RuntimeAccessRequest(WORKSPACE_ID, role, binding_id or uuid4())


def _permission(
    request: RuntimeAccessRequest,
    connection: ModelConnectionRevision,
    *,
    protocols: frozenset[str] = frozenset({"openai_compatible"}),
    origins: frozenset[str] = frozenset({ORIGIN}),
) -> BindingPermission:
    return BindingPermission(request, connection.ref, protocols, origins)


def _install_managed(
    catalog: MemoryCatalog,
    store: FakeSecretStore,
    credential_id: UUID,
    secret: str,
    *,
    alias: str = "runtime key",
) -> CredentialMetadata:
    credential = _credential(credential_id, alias=alias)
    catalog.credentials[credential.ref] = credential
    store.put_new(credential.ref, SecretStr(secret))
    return credential


class TrackingSources(Mapping[str, SecretStr]):
    def __init__(self, values: dict[str, SecretStr]) -> None:
        self.values = values
        self.lookups: list[str] = []
        self.iterations = 0

    def __getitem__(self, key: str) -> SecretStr:
        self.lookups.append(key)
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


def test_each_allowed_role_resolves_its_own_secret() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    reader = FakeBindingPermissionReader()
    roles_and_keys = (
        ("main_agent", "cfg2-fake-main-key"),
        ("worker", "cfg2-fake-worker-key"),
        ("planner", "cfg2-fake-planner-key"),
    )
    requests = []
    refs = []
    for index, (role, secret) in enumerate(roles_and_keys, start=1):
        connection_id = UUID(int=200 + index)
        credential = _install_managed(
            catalog, store, UUID(int=300 + index), secret
        )
        connection = _connection(connection_id, credential.ref, role)
        catalog.create_connection(connection)
        request = _request(role, UUID(int=400 + index))
        reader.permissions[request] = _permission(request, connection)
        requests.append(request)
        refs.append(credential.ref)

    resolver = _resolver(catalog, reader, store, {})

    resolved = [resolver.resolve(request) for request in requests]

    assert [item.secret.get_secret_value() for item in resolved] == [
        secret for _role, secret in roles_and_keys
    ]
    assert [item.credential.ref for item in resolved] == refs
    assert [ref for action, ref in store.calls if action == "read"] == refs


def test_denied_worker_binding_never_reads_main_secret() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential = _install_managed(
        catalog, store, UUID(int=301), "cfg2-fake-main-key"
    )
    main_connection = _connection(UUID(int=201), credential.ref, "main_agent")
    catalog.create_connection(main_connection)
    main_request = _request("main_agent", UUID(int=401))
    worker_request = _request("worker", UUID(int=402))
    reader = FakeBindingPermissionReader(
        {main_request: _permission(main_request, main_connection)}
    )
    sources = TrackingSources(
        {"DEEPSEEK_API_KEY": SecretStr("cfg2-fake-unrelated-deepseek-key")}
    )
    resolver = _resolver(catalog, reader, store, sources)

    with pytest.raises(ConfigurationError) as denied:
        resolver.resolve(worker_request)

    assert denied.value.code == "runtime_binding_denied"
    assert not any(action == "read" for action, _ref in store.calls)
    assert sources.lookups == []
    assert sources.iterations == 0


def test_permission_for_different_request_is_denied_before_source_read() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential = _install_managed(
        catalog, store, UUID(int=302), "cfg2-fake-main-key"
    )
    connection = _connection(UUID(int=202), credential.ref, "main_agent")
    catalog.create_connection(connection)
    authorized_request = _request("main_agent", UUID(int=403))
    changed_request = RuntimeAccessRequest(
        WORKSPACE_ID, "worker", authorized_request.binding_id
    )
    permission = _permission(authorized_request, connection)

    class StalePermissionReader:
        def authorize(self, request: RuntimeAccessRequest) -> BindingPermission:
            return permission

    sources = TrackingSources(
        {"DEEPSEEK_API_KEY": SecretStr("cfg2-fake-unrelated-deepseek-key")}
    )
    resolver = _resolver(catalog, StalePermissionReader(), store, sources)

    with pytest.raises(ConfigurationError) as denied:
        resolver.resolve(changed_request)

    assert denied.value.code == "runtime_binding_denied"
    assert not any(action == "read" for action, _ref in store.calls)
    assert sources.lookups == []
    assert sources.iterations == 0


def test_permission_for_different_workspace_is_denied_before_source_read() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential = _install_managed(
        catalog, store, UUID(int=309), "cfg2-fake-main-key"
    )
    connection = _connection(UUID(int=210), credential.ref, "main_agent")
    catalog.create_connection(connection)
    authorized_request = _request("main_agent", UUID(int=411))
    other_workspace_request = RuntimeAccessRequest(
        UUID(int=999), "main_agent", authorized_request.binding_id
    )
    permission = _permission(authorized_request, connection)

    class StalePermissionReader:
        def authorize(self, request: RuntimeAccessRequest) -> BindingPermission:
            return permission

    sources = TrackingSources(
        {"DEEPSEEK_API_KEY": SecretStr("cfg2-fake-unrelated-deepseek-key")}
    )
    resolver = _resolver(catalog, StalePermissionReader(), store, sources)

    with pytest.raises(ConfigurationError) as denied:
        resolver.resolve(other_workspace_request)

    assert denied.value.code == "runtime_binding_denied"
    assert not any(action == "read" for action, _ref in store.calls)
    assert sources.lookups == []
    assert sources.iterations == 0


def test_deny_all_reader_never_reads_a_configured_secret() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential = _install_managed(
        catalog, store, UUID(int=303), "cfg2-fake-denied-key"
    )
    connection = _connection(UUID(int=203), credential.ref, "main_agent")
    catalog.create_connection(connection)
    sources = TrackingSources(
        {"DEEPSEEK_API_KEY": SecretStr("cfg2-fake-unrelated-deepseek-key")}
    )
    resolver = _resolver(
        catalog,
        DenyAllBindingPermissionReader(),
        store,
        sources,
    )

    with pytest.raises(ConfigurationError) as denied:
        resolver.resolve(_request("main_agent"))

    assert denied.value.code == "runtime_binding_denied"
    assert not any(action == "read" for action, _ref in store.calls)
    assert sources.lookups == []
    assert sources.iterations == 0


def test_role_protocol_and_origin_denials_happen_before_secret_read() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential = _install_managed(
        catalog, store, UUID(int=304), "cfg2-fake-target-key"
    )
    connection = _connection(UUID(int=204), credential.ref, "main_agent")
    catalog.create_connection(connection)
    worker_request = _request("worker")
    protocol_request = _request("main_agent")
    origin_request = _request("main_agent")
    cases = (
        (
            worker_request,
            _permission(worker_request, connection),
            "connection_role_denied",
        ),
        (
            protocol_request,
            _permission(protocol_request, connection, protocols=frozenset()),
            "connection_target_denied",
        ),
        (
            origin_request,
            _permission(
                origin_request,
                connection,
                origins=frozenset({"https://other.example.test"}),
            ),
            "connection_target_denied",
        ),
    )

    for request, permission, code in cases:
        sources = TrackingSources(
            {"DEEPSEEK_API_KEY": SecretStr("cfg2-fake-unrelated-deepseek-key")}
        )
        resolver = _resolver(
            catalog,
            FakeBindingPermissionReader({request: permission}),
            store,
            sources,
        )
        with pytest.raises(ConfigurationError) as rejected:
            resolver.resolve(request)
        assert rejected.value.code == code
        assert sources.lookups == []
        assert sources.iterations == 0

    assert not any(action == "read" for action, _ref in store.calls)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("pending", "credential_unresolved"),
        ("failed", "credential_unresolved"),
        ("revoked", "credential_revoked"),
    ],
)
def test_credential_status_is_checked_before_secret_read(status: str, code: str) -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential = _install_managed(
        catalog, store, UUID(int=305), "cfg2-fake-inactive-key"
    )
    catalog.credentials[credential.ref] = replace(credential, status=status)
    connection = _connection(UUID(int=205), credential.ref, "worker")
    catalog.create_connection(connection)
    request = _request("worker")
    sources = TrackingSources(
        {"DEEPSEEK_API_KEY": SecretStr("cfg2-fake-unrelated-deepseek-key")}
    )
    resolver = _resolver(
        catalog,
        FakeBindingPermissionReader({request: _permission(request, connection)}),
        store,
        sources,
    )

    with pytest.raises(ConfigurationError) as rejected:
        resolver.resolve(request)

    assert rejected.value.code == code
    assert not any(action == "read" for action, _ref in store.calls)
    assert sources.lookups == []
    assert sources.iterations == 0


def test_env_resolution_reads_only_the_selected_registered_name() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential = _credential(
        UUID(int=306), source="env", env_name="SIDERA_MODEL_KEY"
    )
    catalog.credentials[credential.ref] = credential
    connection = _connection(UUID(int=206), credential.ref, "worker")
    catalog.create_connection(connection)
    request = _request("worker")
    sources = TrackingSources(
        {
            "SIDERA_MODEL_KEY": SecretStr("cfg2-fake-selected-env-key"),
            "DEEPSEEK_API_KEY": SecretStr("cfg2-fake-unrelated-deepseek-key"),
        }
    )
    resolver = _resolver(
        catalog,
        FakeBindingPermissionReader({request: _permission(request, connection)}),
        store,
        sources,
    )

    resolved = resolver.resolve(request)

    assert resolved.secret.get_secret_value() == "cfg2-fake-selected-env-key"
    assert sources.lookups == ["SIDERA_MODEL_KEY"]
    assert sources.iterations == 0
    assert store.calls == []


def test_missing_selected_env_name_does_not_fall_back_to_deepseek_key() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential = _credential(
        UUID(int=307), source="env", env_name="SIDERA_MODEL_KEY"
    )
    catalog.credentials[credential.ref] = credential
    connection = _connection(UUID(int=207), credential.ref, "worker")
    catalog.create_connection(connection)
    request = _request("worker")
    sources = TrackingSources(
        {"DEEPSEEK_API_KEY": SecretStr("cfg2-fake-unrelated-deepseek-key")}
    )
    resolver = _resolver(
        catalog,
        FakeBindingPermissionReader({request: _permission(request, connection)}),
        store,
        sources,
    )

    with pytest.raises(ConfigurationError) as unresolved:
        resolver.resolve(request)

    assert unresolved.value.code == "credential_unresolved"
    assert sources.lookups == ["SIDERA_MODEL_KEY"]
    assert sources.iterations == 0
    assert store.calls == []


def test_keychain_lookup_uses_exact_ref_not_native_store_alias() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential = _install_managed(
        catalog,
        store,
        UUID(int=308),
        "cfg2-fake-managed-key",
        alias="store:native/openai",
    )
    connection = _connection(UUID(int=208), credential.ref, "worker")
    catalog.create_connection(connection)
    request = _request("worker")
    resolver = _resolver(
        catalog,
        FakeBindingPermissionReader({request: _permission(request, connection)}),
        store,
        {},
    )

    resolved = resolver.resolve(request)

    assert resolved.secret.get_secret_value() == "cfg2-fake-managed-key"
    assert [call for call in store.calls if call[0] == "read"] == [
        ("read", credential.ref)
    ]


def test_rotation_keeps_old_binding_and_new_revision_gets_new_key_then_revokes() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    credential_service = CredentialService(catalog, store)
    first_write = credential_service.save_managed(
        WORKSPACE_ID, uuid4(), "worker key", SecretStr("cfg2-fake-old-rotation-key")
    )
    first = first_write.credential
    connection = _connection(UUID(int=209), first.ref, "worker")
    saved_connection = ConnectionService(catalog).create(connection)
    old_request = _request("worker", UUID(int=409))
    reader = FakeBindingPermissionReader(
        {old_request: _permission(old_request, saved_connection)}
    )
    resolver = _resolver(catalog, reader, store, {})

    original = resolver.resolve(old_request)
    rotated_write = credential_service.save_managed(
        WORKSPACE_ID,
        uuid4(),
        "worker key",
        SecretStr("cfg2-fake-new-rotation-key"),
        replaces_ref=first.ref,
    )
    old_after_rotation = resolver.resolve(old_request)
    next_connection = replace(
        saved_connection,
        ref=ConnectionRef(WORKSPACE_ID, saved_connection.ref.connection_id, 2),
        credential_ref=rotated_write.credential.ref,
        allowed_roles=frozenset({"worker", "planner"}),
    )
    revised_connection = ConnectionService(catalog).revise(
        next_connection, expected_revision=1
    )
    new_request = _request("planner", UUID(int=410))
    reader.permissions[new_request] = _permission(new_request, revised_connection)
    new_binding = resolver.resolve(new_request)
    reads_before_revoke = sum(action == "read" for action, _ref in store.calls)

    credential_service.revoke(first.ref, expected_version=first.version)

    with pytest.raises(ConfigurationError) as revoked:
        resolver.resolve(old_request)

    assert revoked.value.code == "credential_revoked"
    assert original.secret.get_secret_value() == "cfg2-fake-old-rotation-key"
    assert old_after_rotation.secret.get_secret_value() == "cfg2-fake-old-rotation-key"
    assert new_binding.secret.get_secret_value() == "cfg2-fake-new-rotation-key"
    assert catalog.get_connection(saved_connection.ref) == saved_connection
    assert catalog.get_connection(revised_connection.ref) == revised_connection
    assert sum(action == "read" for action, _ref in store.calls) == reads_before_revoke
